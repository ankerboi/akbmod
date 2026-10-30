"""Multiplayer: Steam Spacewar lobbies when available, else LAN / virtual-LAN rooms.

Matches the Rust rewrite (steam.rs / net.rs):
  * rooms are listed with Create / Refresh / Join (marker akb = AKBARENA3)
  * Steam app id 480 (Spacewar) when steam_api is present
  * otherwise UDP discovery + peer-to-peer (LAN, Radmin VPN, FreeLAN, Join by IP)
  * no dedicated game server — players talk to each other
"""
import json
import os
import random
import socket
import struct
import threading
import time
from pathlib import Path

from .config import CFG
try:
    from .glutil import FX
except Exception:             # headless dedicated server: no OpenGL needed, only the day/night flag
    class FX:
        day = False

try:
    from .steam import APP_ID as STEAM_APP_ID, MAGIC as STEAM_MAGIC, MAX_PLAYERS as STEAM_MAX, get_steam
except Exception:
    STEAM_APP_ID, STEAM_MAGIC, STEAM_MAX = 480, "AKBARENA3", 8
    def get_steam():
        return None


NET_DISC_PORT = 27777          # LAN broadcast (room discovery)
NET_GAME_PORT = 27778          # preferred UDP game port (any free one is used if busy)
NET_MAGIC = "AKBARENA3"        # same marker as the Rust / Spacewar lobbies
NET_PROTO = 3                  # bumped with AKBARENA3 room fields
NET_MAX_PLAYERS = 8            # match Rust MAX_PLAYERS
NET_STATE_HZ = 30              # state packets per second (Rust SEND_HZ)
NET_DROPPABLE = ("st", "shot")  # sent without resend, newest state wins

JOIN_TIMEOUT = 30.0            # seconds a join may take before giving up
JOIN_RETRY = 9.0               # new socket + new attempt every this many seconds while nothing answers
def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # routing lookup only, nothing is sent
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def local_ips():
    """Every IPv4 address of this PC (LAN adapter + virtual LAN adapters)."""
    out = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in out and not ip.startswith(("127.", "169.254.")):
                out.append(ip)
    except Exception:
        pass
    ip = lan_ip()
    if ip not in out and not ip.startswith("127."):
        out.append(ip)
    return out


class UConn:
    """A tiny reliable channel on top of a shared UDP socket (what TCP gave us before).
    Reliable messages carry a sequence number, are resent until acknowledged and are
    delivered in order. Droppable ones (player state, shots) are fire-and-forget.
    Until the first packet arrives it talks to every candidate address at once."""
    RTO = 0.2

    def __init__(self, sock, addrs):
        self.s = sock
        self.addrs = list(addrs)
        self.locked = False       # True once we know which address really answers
        self.dead = False
        self.pid = -1
        self.cid = ""             # host side: which joining attempt this is (dedupes multi-path hellos)
        self.jid = ""             # host side: which join this is (stays the same when the joiner retries)
        self.last = time.time()
        self.seq = 0              # last reliable seq sent
        self.unacked = {}         # seq -> [packet, last send time]
        self.rseq = 0             # last reliable seq received in order
        self.ooo = {}             # reliable packets that arrived early
        self.inbox = []
        self.t_ping = 0.0

    def accepts(self, addr):
        if not self.locked:
            return True   # still probing: accept a reply from any path the host uses
        return addr in self.addrs

    def _raw(self, obj):
        data = json.dumps(obj, separators=(",", ":")).encode()
        for a in self.addrs:
            try:
                self.s.sendto(data, a)
            except OSError:
                pass

    def send(self, msg, droppable=False):
        if self.dead:
            return
        if droppable:
            self._raw({"a": self.rseq, "m": msg})
            return
        self.seq += 1
        pkt = {"q": self.seq, "a": self.rseq, "m": msg}
        self.unacked[self.seq] = [pkt, time.time()]
        if len(self.unacked) > 500:     # peer is not acknowledging anything
            self.dead = True
            return
        self._raw(pkt)

    def flush(self):
        if self.dead:
            return
        now = time.time()
        for ent in self.unacked.values():
            if now - ent[1] >= self.RTO:
                ent[1] = now
                ent[0]["a"] = self.rseq
                self._raw(ent[0])
        if now - self.t_ping >= 1.0:    # keepalive: holds the NAT hole open, carries acks
            self.t_ping = now
            self._raw({"a": self.rseq})

    def feed(self, pkt, addr):
        self.last = time.time()
        if not self.locked:
            self.locked = True
            self.addrs = [addr]
        elif addr not in self.addrs:
            # host/NAT may reply from a slightly different mapped port — keep talking to it
            self.addrs = [addr]
        a = pkt.get("a")
        if isinstance(a, int) and self.unacked:
            for q in [q for q in self.unacked if q <= a]:
                del self.unacked[q]
        m = pkt.get("m")
        if not isinstance(m, dict):
            return
        if m.get("t") == "bye":
            self.dead = True
            return
        q = pkt.get("q")
        if q is None:
            self.inbox.append(m)
            return
        if not isinstance(q, int):
            return
        if q == self.rseq + 1:
            self.rseq = q
            self.inbox.append(m)
            while self.rseq + 1 in self.ooo:
                self.rseq += 1
                self.inbox.append(self.ooo.pop(self.rseq))
        elif q > self.rseq + 1 and len(self.ooo) < 300:
            self.ooo[q] = m
        self._raw({"a": self.rseq})     # acknowledge straight away

    def read(self):
        out, self.inbox = self.inbox, []
        return out

    def bye(self):
        for _ in range(3):
            self._raw({"a": self.rseq, "m": {"t": "bye"}})

    def close(self):
        self.dead = True


class _Mux:
    """What UConn sends through: always the current UDP game socket (it can be swapped on a retry)."""

    def __init__(self, net):
        self.net = net

    def sendto(self, data, addr):
        s = self.net.sock
        if s is None:
            raise OSError("no socket")
        return s.sendto(data, addr)


class Net:
    def __init__(self):
        self.mux = _Mux(self)
        self.mode = None          # None | "host" | "joining" | "client"
        self.room = ""
        self.rid = ""
        self.my_id = 0
        self.my_team = 0
        self.dedicated = False    # host only: a headless server that is not a player itself
        self.max_players = NET_MAX_PLAYERS
        self.players = {}         # everyone else: id -> {"n": name, "tm": team}
        self.peers = {}           # host: id -> UConn
        self.pending = []         # host: connections that have not said hello yet
        self.by_addr = {}         # host: source address -> UConn
        self.sock = None          # the one UDP game socket
        self.port = 0
        self.conn = None          # client: link to the host
        self.t_ann = 0.0
        self.host_t0 = 0.0
        self.join_t0 = 0.0
        self.cid = "%08x" % random.getrandbits(32)
        self.lan_rooms = {}       # rooms found by UDP broadcast on this network
        self.udp = None           # LAN discovery socket
        self.status = ""
        self.t_bc = 0.0
        self.t_st = 0.0
        self.next_id = 1
        self.map_sig = "none"
        self.pname = "Player"
        self.score_cb = lambda: [0, 0]
        # True when traffic goes through Steam's relay (slower state rate). LAN / direct P2P = False.
        self.relayed = False

    @property
    def active(self):
        return self.mode in ("host", "client")

    @property
    def rooms(self):
        """Open rooms for the Play tab (Create / Refresh / Join), newest first."""
        return dict(self.lan_rooms)

    def room_list(self):
        """Stable list of rooms for the menu (same shape as Rust RoomInfo)."""
        rows = list(self.lan_rooms.values())
        rows.sort(key=lambda r: (-r.get("seen", 0), r.get("n", "")))
        return rows

    def try_steam(self):
        """Connect to Steam as Spacewar when the API library is present."""
        st = get_steam()
        if st is None:
            return False
        if st.available:
            return True
        ok = st.connect()
        if ok:
            self.status = st.status_hint
        return ok

    # ----- LAN discovery (UDP broadcast) -----
    def start_discovery(self):
        if self.udp:
            return
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.bind(("", NET_DISC_PORT))
            s.setblocking(False)
            self.udp = s
        except OSError:
            self.udp = None   # no room list then; Direct join by IP still works

    def _disc(self, now):
        if self.udp:
            for _ in range(32):
                try:
                    data, addr = self.udp.recvfrom(2048)
                except (BlockingIOError, InterruptedError, OSError):
                    break
                try:
                    j = json.loads(data.decode("utf-8", "ignore"))
                except ValueError:
                    continue
                if not isinstance(j, dict) or j.get("g") != NET_MAGIC or j.get("id") == self.rid:
                    continue
                # accept AKBARENA3 (and older AKBARENA2) room beacons — same fields as Steam RoomInfo
                try:
                    rid = j["id"]
                    self.lan_rooms[rid] = {
                        "id": rid,
                        "lobby": rid,  # same id used as lobby key in the menu
                        "n": str(j.get("n", ""))[:40],
                        "name": str(j.get("n", ""))[:40],
                        "h": str(j.get("h", ""))[:24],
                        "host_name": str(j.get("h", ""))[:24],
                        "p": int(j.get("p", 1)),
                        "players": int(j.get("p", 1)),
                        "m": int(j.get("m", NET_MAX_PLAYERS)),
                        "max": int(j.get("m", NET_MAX_PLAYERS)),
                        "ip": addr[0],
                        "port": int(j.get("port", NET_GAME_PORT)),
                        "map": str(j.get("map", "")),
                        "seen": now,
                        "src": "lan",
                        "lan": None,
                        "akb": NET_MAGIC,
                    }
                except (KeyError, ValueError, TypeError):
                    continue
            if self.mode == "host" and now - self.t_bc >= 1.0:
                self.t_bc = now
                pkt = json.dumps({"g": NET_MAGIC, "id": self.rid, "n": self.room, "h": self.pname,
                                  "p": self.nplayers(), "m": self.max_players, "port": self.port,
                                  "map": self.map_sig}, separators=(",", ":")).encode()
                targets = ["255.255.255.255"]
                for ip in local_ips():       # every adapter, so a virtual LAN (Radmin VPN, FreeLAN) gets the broadcast too
                    t = ".".join(ip.split(".")[:3] + ["255"])
                    if t not in targets:
                        targets.append(t)
                for t in targets:
                    try:
                        self.udp.sendto(pkt, (t, NET_DISC_PORT))
                    except OSError:
                        pass
        for k in [k for k, r in self.lan_rooms.items() if now - r["seen"] > 4.0]:
            del self.lan_rooms[k]

    # ----- sockets -----
    def _open_udp(self, port=0, tries=1):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for p in range(port, port + tries):
            try:
                s.bind(("", p))
                break
            except OSError:
                continue
        else:
            s.close()
            return None
        s.setblocking(False)
        if os.name == "nt":
            try:
                s.ioctl(0x9800000C, False)     # SIO_UDP_CONNRESET: ignore ICMP "port unreachable" errors
            except Exception:
                pass
        return s

    def _pump(self, now):
        """Read every waiting datagram and hand it to the right connection."""
        for _ in range(512):
            try:
                data, addr = self.sock.recvfrom(4096)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                if self.sock is None or self.sock.fileno() < 0:
                    break
                continue
            try:
                pkt = json.loads(data)
            except ValueError:
                continue
            self._route(pkt, addr)

    def _route(self, pkt, addr):
        if not isinstance(pkt, dict):
            return
        if "p" in pkt:        # stray keep-alive noise
            return
        c = None
        if self.mode == "host":
            c = self.by_addr.get(addr)
            if c is None:
                m = pkt.get("m")
                if (isinstance(m, dict) and m.get("t") == "hello" and pkt.get("q") == 1
                        and len(self.pending) < 16):
                    c = UConn(self.mux, [addr])
                    c.locked = True
                    self.by_addr[addr] = c
                    self.pending.append(c)
        elif self.conn is not None and self.conn.accepts(addr):
            c = self.conn
        if c is not None:
            c.feed(pkt, addr)

    # ----- host / join / leave -----
    def nplayers(self):
        return len(self.peers) + (0 if self.dedicated else 1)

    def host(self, room, pname, map_sig, team=0, dedicated=False, max_players=NET_MAX_PLAYERS):
        self.leave()
        self.dedicated, self.max_players = bool(dedicated), int(max_players)
        self.start_discovery()
        sock = self._open_udp(NET_GAME_PORT, 20)
        if sock is None:
            self.status = "Could not open a network port for the server."
            return False
        self.sock = sock
        self.port = sock.getsockname()[1]
        self.mode = "host"
        self.room = room
        self.rid = "%08x" % random.getrandbits(32)
        self.my_id = 0
        self.my_team = team
        self.players, self.peers, self.pending, self.by_addr = {}, {}, [], {}
        self.next_id = 1
        self.map_sig, self.pname = map_sig, pname
        self.host_t0 = time.time()
        self.status = "Room '%s' open (Spacewar-style / LAN). Friends on your network see it under Refresh. Address %s:%d" % (room, lan_ip(), self.port)
        return True

    @staticmethod
    def room_cands(r):
        """Address to try for a room-list entry."""
        try:
            return [(str(r["ip"]), int(r["port"]))]
        except (KeyError, TypeError, ValueError):
            return []

    def join_room(self, r, pname, map_sig):
        return self.join(self.room_cands(r), pname, map_sig)

    def join(self, cands, pname, map_sig):
        self.leave()
        self.start_discovery()
        sock = self._open_udp()
        if sock is None:
            self.status = "Cannot connect: no free network port."
            return False
        self.sock = sock
        self.port = sock.getsockname()[1]
        self.mode = "joining"
        self.pname, self.map_sig = pname, map_sig
        self.join_t0 = time.time()
        self._join_sec = -1
        self.jid = "%08x" % random.getrandbits(32)
        self.cid = "%08x" % random.getrandbits(32)
        self._jcands = list(cands)
        self.conn = UConn(self.mux, cands)
        self.conn.send(self._hello())
        self.status = "Connecting to the server (up to %ds)..." % JOIN_TIMEOUT
        return True

    def _hello(self):
        return {"t": "hello", "g": NET_MAGIC, "v": NET_PROTO, "n": self.pname, "map": self.map_sig,
                "cid": self.cid, "jid": self.jid}

    def leave(self, status=None):
        for c in list(self.peers.values()):
            c.bye()
        if self.conn:
            self.conn.bye()
        for c in list(self.peers.values()) + list(self.pending):
            c.close()
        if self.conn:
            self.conn.close()
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = self.conn = None
        self.peers, self.pending, self.by_addr, self.players = {}, [], {}, {}
        self.mode = None
        self.rid = ""
        self.my_id = 0
        self.relayed = False
        if status is not None:
            self.status = status

    def close(self):
        self.leave()
        if self.udp:
            try:
                self.udp.close()
            except OSError:
                pass
            self.udp = None

    # ----- send -----
    def send(self, m, droppable=False):
        """Broadcast to the room, or to one player when m has "to"."""
        if self.mode == "host":
            m["f"] = 0
            to = m.get("to")
            if to is None:
                for c in self.peers.values():
                    c.send(m, droppable)
            else:
                c = self.peers.get(to)
                if c:
                    c.send(m, droppable)
        elif self.mode == "client" and self.conn:
            self.conn.send(m, droppable)

    # ----- receive -----
    def _team_for_new(self):
        cnt = [0, 0]
        if not self.dedicated:
            cnt[self.my_team] += 1
        for p in self.players.values():
            cnt[p["tm"] if p["tm"] in (0, 1) else 0] += 1
        return 0 if cnt[0] < cnt[1] else 1

    def _forget(self, c):
        if c.addrs:
            self.by_addr.pop(c.addrs[0], None)
        if c in self.pending:
            self.pending.remove(c)

    def _drop(self, c, msgs, why=""):
        c.bye()
        c.close()
        self._forget(c)
        if c.pid in self.peers:
            del self.peers[c.pid]
            self.players.pop(c.pid, None)
            m = {"t": "leave", "id": c.pid, "f": 0}
            for o in self.peers.values():
                o.send(m)
            msgs.append(m)

    def poll(self, now):
        msgs = []
        self._disc(now)
        if self.sock:
            self._pump(now)
        if self.mode == "host":
            self._poll_host(now, msgs)
        elif self.mode == "joining":
            self._poll_joining(now, msgs)
        elif self.mode == "client":
            self._poll_client(now, msgs)
        return msgs

    def _poll_host(self, now, msgs):
        for c in list(self.pending):
            for m in c.read():
                if m.get("t") != "hello":
                    continue
                name = str(m.get("n", "Player"))[:16] or "Player"
                cid = str(m.get("cid", ""))[:16]
                jid = str(m.get("jid", ""))[:16]
                if cid and any(p.cid == cid for p in self.peers.values()):
                    self._forget(c)     # same joiner reached us over a second route: keep the first
                    c.close()
                    break
                if jid:
                    for old in [p for p in self.peers.values() if p.jid == jid]:
                        self._drop(old, msgs)       # same join, retried on a fresh socket: the old link is a ghost
                why = None
                if m.get("v") not in (2, 3, NET_PROTO) or m.get("g") not in (NET_MAGIC, "AKBARENA2"):
                    why = "Game version mismatch."
                elif self.nplayers() >= self.max_players:
                    why = "Server is full."
                elif m.get("map") != self.map_sig:
                    why = f"Different map. The host uses '{self.map_sig}', you have '{m.get('map')}'. Load the same map first."
                if why:
                    c.send({"t": "deny", "why": why, "map": self.map_sig})
                    for _ in range(2):
                        c.unacked and c._raw(list(c.unacked.values())[-1][0])
                    self._forget(c)
                    c.close()
                else:
                    pid = self.next_id
                    self.next_id += 1
                    tm = self._team_for_new()
                    roster = ([] if self.dedicated else [{"id": 0, "n": self.pname, "tm": self.my_team}]) + \
                             [{"id": i, "n": p["n"], "tm": p["tm"]} for i, p in self.players.items()]
                    c.pid = pid
                    c.cid = cid
                    c.jid = jid
                    c.send({"t": "welcome", "id": pid, "team": tm, "ps": roster, "room": self.room,
                            "sc": self.score_cb(), "frags": CFG["frags"], "day": int(FX.day)})
                    j = {"t": "join", "id": pid, "n": name, "tm": tm, "f": 0}
                    for o in self.peers.values():
                        o.send(j)
                    self.pending.remove(c)
                    self.peers[pid] = c
                    self.players[pid] = {"n": name, "tm": tm}
                    msgs.append(j)
                break
            if c in self.pending:
                if c.dead or now - c.last > 6:
                    self._forget(c)
                    c.close()
                else:
                    c.flush()
        for c in list(self.peers.values()):
            for m in c.read():
                t = m.get("t")
                if t in ("hello", "welcome", "join", "leave", "deny"):
                    continue
                m["f"] = c.pid
                to = m.get("to")
                if to is not None and to != 0:
                    o = self.peers.get(to)
                    if o:
                        o.send(m, droppable=(t in NET_DROPPABLE))
                    continue
                if to is None:
                    for o in self.peers.values():
                        if o is not c:
                            o.send(m, droppable=(t in NET_DROPPABLE))
                msgs.append(m)
            c.flush()
            if c.dead or now - c.last > 10:
                self._drop(c, msgs)

    def _poll_joining(self, now, msgs):
        c = self.conn
        c.flush()
        sec = int(now - self.join_t0)
        if sec != self._join_sec:
            self._join_sec = sec
            self.status = "Connecting to the server (%ds / %ds)..." % (sec, JOIN_TIMEOUT)
        for m in c.read():
            t = m.get("t")
            if t == "welcome":
                self.mode = "client"
                self.my_id = int(m.get("id", 1))
                self.my_team = int(m.get("team", 1)) & 1
                self.room = str(m.get("room", ""))
                self.players = {int(p["id"]): {"n": str(p["n"])[:16], "tm": int(p["tm"]) & 1}
                                for p in m.get("ps", [])}
                CFG["frags"] = int(m.get("frags", CFG["frags"]))
                FX.day = bool(m.get("day", 0))       # the host picks night / day for the map
                self.status = f"Joined '{self.room}'."
                msgs.append(m)
            elif t == "deny":
                self.leave("Denied: " + str(m.get("why", "Denied.")))
                msgs.append({"t": "failed", "map": str(m.get("map", "")), "cands": list(self._jcands)})
                return
        if self.mode == "joining" and (c.dead or now - self.join_t0 > JOIN_TIMEOUT):
            why = ("Could not reach the host. Check that you are on the same network (or the same Radmin VPN / "
                   "virtual LAN), that the address is right, and that the firewall allows the game (UDP).")
            self.leave(why)
            msgs.append({"t": "failed"})

    def _poll_client(self, now, msgs):
        c = self.conn
        for m in c.read():
            t = m.get("t")
            if t == "join":
                self.players[int(m["id"])] = {"n": str(m.get("n", "?"))[:16], "tm": int(m.get("tm", 0)) & 1}
            elif t == "leave":
                self.players.pop(int(m.get("id", -1)), None)
            elif t in ("welcome", "deny"):
                continue
            msgs.append(m)
        c.flush()
        if c.dead or now - c.last > 10:
            self.leave("Lost connection to the host.")
            msgs.append({"t": "closed", "why": "Lost connection to the host."})


NET = Net()

