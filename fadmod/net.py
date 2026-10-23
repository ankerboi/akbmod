"""LAN discovery, MQTT broker room browser, UDP peer networking."""
import json
import queue
import random
import socket
import struct
import threading
import time
from pathlib import Path


NET_DISC_PORT = 27777          # LAN broadcast
NET_GAME_PORT = 27778          # preferred UDP game port (any free one is used if busy)
NET_MAGIC = "AKBARENA2"
NET_PROTO = 2
NET_MAX_PLAYERS = 6
NET_STATE_HZ = 40      # how often your position is sent online (was 25)
NET_DROPPABLE = ("st", "shot", "shove")  # sent without resend, newest state wins

BROKER_ROOT = "akbarena-v2"
BROKER_HOSTS = ("broker.hivemq.com", "broker.emqx.io", "test.mosquitto.org")   # free, no account, tried in order
STUN_HOSTS = (("stun.l.google.com", 19302), ("stun1.l.google.com", 19302),
              ("stun.cloudflare.com", 3478), ("stun.nextcloud.com", 3478))
STUN_COOKIE = 0x2112A442

def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # routing lookup only, nothing is sent
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


class Broker:
    """Free public MQTT brokers used as the phone book (room list) and as the signalling
    channel for NAT hole punching. We connect to ALL of them at the same time (one thread
    each) and publish / subscribe on every one that is up, so a slow or dead broker never
    delays you and two players can never end up on different brokers.
    Results arrive in self.q (duplicates from several brokers are harmless)."""

    def __init__(self):
        self.clis = {}            # host -> paho client
        self.okh = set()          # hosts that are connected right now
        self.err = ""
        self.q = queue.Queue()
        self.want = set()
        self.threads = []
        self.stop = False

    @property
    def ok(self):
        return bool(self.okh)

    def start(self):
        if self.threads:
            return
        for h in BROKER_HOSTS:
            t = threading.Thread(target=self._run, args=(h,), daemon=True)
            t.start()
            self.threads.append(t)

    def _run(self, host):
        try:
            import paho.mqtt.client as mqtt
        except Exception:
            self.err = "paho-mqtt missing (pip install paho-mqtt)"
            return
        cid = "akb-%012x" % random.getrandbits(48)
        while not self.stop:
            try:
                try:
                    cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid, userdata=host)   # paho 2.x
                except AttributeError:
                    cli = mqtt.Client(client_id=cid, userdata=host)                                      # paho 1.x
                cli.on_connect = self._on_connect
                cli.on_disconnect = self._on_disc
                cli.on_message = self._on_msg
                try:
                    cli.reconnect_delay_set(min_delay=1, max_delay=5)
                except Exception:
                    pass
                self.clis[host] = cli
                cli.connect(host, 1883, keepalive=20)      # paho's own connect timeout is 5s
            except Exception as ex:
                self.clis.pop(host, None)
                if not self.okh:
                    self.err = "%s: %s" % (host, str(ex)[:40])
                time.sleep(3.0)
                continue
            cli.loop_start()      # keeps running and reconnects by itself
            while not self.stop:
                time.sleep(1.0)
            try:
                cli.loop_stop()
                cli.disconnect()
            except Exception:
                pass
            return

    def _on_connect(self, c, host, flags, rc, *a):
        good = (rc == 0) or (str(rc) == "Success")
        if good:
            self.okh.add(host)
            self.err = ""
            for t in list(self.want):
                try:
                    c.subscribe(t, qos=0)
                except Exception:
                    pass
        else:
            self.okh.discard(host)
            if not self.okh:
                self.err = "%s refused (%s)" % (host, rc)

    def _on_disc(self, c, host, *a):
        self.okh.discard(host)

    def _on_msg(self, c, u, msg):
        try:
            j = json.loads(msg.payload.decode("utf-8", "ignore")) if msg.payload else None
        except ValueError:
            return
        # retain=True: this is the broker's stored copy (delivered on subscribe), not a live announce
        self.q.put((msg.topic, j, bool(getattr(msg, "retain", False))))

    def _live(self):
        return [self.clis[h] for h in list(self.okh) if h in self.clis]

    def sub(self, topic):
        self.want.add(topic)
        for c in self._live():
            try:
                c.subscribe(topic, qos=0)
            except Exception:
                pass

    def unsub(self, topic):
        self.want.discard(topic)
        for c in self._live():
            try:
                c.unsubscribe(topic)
            except Exception:
                pass

    def pub(self, topic, obj, retain=False):
        """obj=None with retain=True deletes a retained message. Goes out on every live broker."""
        live = self._live()
        if not live:
            return False
        payload = b"" if obj is None else json.dumps(obj, separators=(",", ":"))
        deleting = (obj is None and retain)
        sent = False
        infos = []
        for c in live:
            try:
                # A retained delete must be confirmed by the broker (QoS 1), otherwise a broker
                # that momentarily misses it keeps the closed room stored forever.
                infos.append(c.publish(topic, payload, qos=1 if deleting else 0, retain=retain))
                sent = True
            except Exception:
                pass
        if deleting:
            # Wait for every PUBACK, then say it a couple more times: any broker that was
            # reconnecting when we first sent the delete would otherwise still show the room.
            deadline = time.time() + 0.4
            for inf in infos:
                try:
                    inf.wait_for_publish(timeout=max(0.05, deadline - time.time()))
                except Exception:
                    pass
            time.sleep(0.05)
            for _ in range(2):
                for c in self._live():
                    try:
                        c.publish(topic, b"", qos=1, retain=True)
                    except Exception:
                        pass
                time.sleep(0.08)
        return sent

    def pub_raw(self, topic, payload):
        """Publish ready-made bytes (relay traffic) on every live broker."""
        sent = False
        for c in self._live():
            try:
                c.publish(topic, payload, qos=0)
                sent = True
            except Exception:
                pass
        return sent

    def close(self):
        self.stop = True
        for c in list(self.clis.values()):
            try:
                c.disconnect()
                c.loop_stop()
            except Exception:
                pass
        self.clis = {}
        self.okh = set()


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
    """What UConn sends through: a real UDP address goes out the UDP socket, an address of
    the form ("relay", key) goes through the MQTT brokers instead (no-NAT-needed fallback)."""

    def __init__(self, net):
        self.net = net

    def sendto(self, data, addr):
        if addr and addr[0] == "relay":
            self.net._relay_send(data, addr)
            return len(data)
        s = self.net.sock
        if s is None:
            raise OSError("no socket")
        return s.sendto(data, addr)


class Net:
    def __init__(self):
        self.mux = _Mux(self)
        self._rts = {}            # room id -> last announce timestamp seen (stale-room filter)
        self._dead = {}           # room id -> wall-clock time we saw the host close it (ignore late copies)
        self.relayed = False      # joiner: this connection runs through the broker relay
        self._rly_n = 0
        self._rly_seen = set()
        self._rly_topics = []
        self.mode = None          # None | "host" | "joining" | "client"
        self.room = ""
        self.rid = ""
        self.my_id = 0
        self.my_team = 0
        self.players = {}         # everyone else: id -> {"n": name, "tm": team}
        self.peers = {}           # host: id -> UConn
        self.pending = []         # host: connections that have not said hello yet
        self.by_addr = {}         # host: source address -> UConn
        self.sock = None          # the one UDP game socket
        self.port = 0
        self.conn = None          # client: link to the host
        self.pub = None           # our public (ip, port) as seen from the internet, via STUN
        self.punch = []           # host: [addr, until, next_send] hole-punch targets
        self.stun_addrs = []
        self.stun_tids = set()
        self.stun_started = False
        self.t_stun = 0.0
        self.t_ann = 0.0
        self.host_t0 = 0.0
        self.pub_warned = False
        self.join_rid = None
        self.join_t0 = 0.0
        self.t_sig = 0.0
        self.cid = "%08x" % random.getrandbits(32)
        self.lan_rooms = {}       # rooms found by UDP broadcast on this network
        self.net_rooms = {}       # rooms found through the internet broker
        self.broker = Broker()
        self.udp = None           # LAN discovery socket
        self.status = ""
        self.t_bc = 0.0
        self.t_st = 0.0
        self.next_id = 1
        self.map_sig = "arena"
        self.pname = "Player"
        self.score_cb = lambda: [0, 0]

    @property
    def active(self):
        return self.mode in ("host", "client")

    @property
    def rooms(self):
        d = dict(self.net_rooms)
        d.update(self.lan_rooms)      # a room seen on both: the direct LAN route wins
        return d

    def start_broker(self):
        self.broker.sub("%s/rooms/+" % BROKER_ROOT)
        self.broker.start()
        self._stun_start()        # resolve the STUN servers now so Host / Join don't wait for DNS

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
            self.udp = None   # fine: the internet room browser still works

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
                try:
                    self.lan_rooms[j["id"]] = {
                        "id": j["id"], "n": str(j.get("n", ""))[:28], "h": str(j.get("h", ""))[:16],
                        "p": int(j.get("p", 1)), "m": int(j.get("m", NET_MAX_PLAYERS)),
                        "ip": addr[0], "port": int(j.get("port", NET_GAME_PORT)),
                        "map": str(j.get("map", "")), "seen": now, "src": "lan", "lan": None,
                    }
                except (KeyError, ValueError, TypeError):
                    continue
            if self.mode == "host" and now - self.t_bc >= 1.0:
                self.t_bc = now
                pkt = json.dumps({"g": NET_MAGIC, "id": self.rid, "n": self.room, "h": self.pname,
                                  "p": len(self.peers) + 1, "m": NET_MAX_PLAYERS, "port": self.port,
                                  "map": self.map_sig}, separators=(",", ":")).encode()
                targets = ["255.255.255.255"]
                ip = lan_ip()
                if ip.count(".") == 3 and not ip.startswith("127."):
                    targets.append(".".join(ip.split(".")[:3] + ["255"]))
                for t in targets:
                    try:
                        self.udp.sendto(pkt, (t, NET_DISC_PORT))
                    except OSError:
                        pass
        for k in [k for k, r in self.lan_rooms.items() if now - r["seen"] > 4.0]:
            del self.lan_rooms[k]
        for k in [k for k, r in self.net_rooms.items() if now - r["seen"] > 7.0]:    # ~3 missed announces = gone
            del self.net_rooms[k]
        if len(self._rts) > 200:
            self._rts.clear()

    # ----- internet broker: room list + hole-punch signalling -----
    def _broker_poll(self, now):
        b = self.broker
        for _ in range(200):
            try:
                topic, j, retained = b.q.get_nowait()
            except queue.Empty:
                break
            parts = topic.split("/")
            if len(parts) >= 4 and parts[1] == "rly":
                self._relay_in(parts, j)
                continue
            if len(parts) != 3:
                continue
            kind, rid = parts[1], parts[2]
            if kind == "rooms":
                if j is None:                     # the host closed the room
                    self.net_rooms.pop(rid, None)
                    self._rts.pop(rid, None)
                    self._dead[rid] = now
                    if len(self._dead) > 200:
                        self._dead.clear()
                    continue
                if not isinstance(j, dict) or j.get("g") != NET_MAGIC or rid == self.rid:
                    continue
                # A room we closed ourselves must never come back, whatever a broker still stores.
                dead_at = self._dead.get(rid)
                if dead_at is not None:
                    if now - dead_at < 300.0:
                        continue
                    self._dead.pop(rid, None)
                try:
                    ts = float(j.get("ts", 0))
                except (TypeError, ValueError):
                    continue
                age = abs(time.time() - ts)
                # A retained message is only the broker's stored copy of the last announcement.
                # The host re-announces every 2 s, so an old retained copy means the host is gone.
                if retained and age > 15.0:
                    continue
                if (not retained) and age > 15.0:
                    # Looks old: either a leftover of a closed room, or the host's PC clock
                    # is off. A live host re-announces every few seconds with a new ts,
                    # a dead room never does - so only show it once its ts changes.
                    if self._rts.get(rid) in (None, ts):
                        self._rts[rid] = ts
                        continue
                self._rts[rid] = ts
                try:
                    pub = j["pub"]
                    lan = j.get("lan")
                    self.net_rooms[rid] = {
                        "id": rid, "n": str(j.get("n", ""))[:28], "h": str(j.get("h", ""))[:16],
                        "p": int(j.get("p", 1)), "m": int(j.get("m", NET_MAX_PLAYERS)),
                        "ip": str(pub[0]), "port": int(pub[1]), "map": str(j.get("map", "")),
                        "lan": [str(lan[0]), int(lan[1])] if lan else None,
                        "seen": now, "src": "net",
                    }
                except (KeyError, ValueError, TypeError, IndexError):
                    continue
            elif kind == "sig" and self.mode == "host" and rid == self.rid:
                if isinstance(j, dict) and j.get("t") == "punch":
                    self._punch_to(j, now)

    def _same_lan(self, ip):
        mine = lan_ip()
        return mine.count(".") == 3 and ip.count(".") == 3 and mine.rsplit(".", 1)[0] == ip.rsplit(".", 1)[0]

    def _punch_to(self, j, now):
        """A joiner told us (through the broker) where it is: fire packets at it so our NAT
        lets its packets in."""
        try:
            addrs = []
            if j.get("pub"):
                addrs.append((str(j["pub"][0]), int(j["pub"][1])))
            if j.get("lan") and self._same_lan(str(j["lan"][0])):
                addrs.append((str(j["lan"][0]), int(j["lan"][1])))
        except (KeyError, ValueError, TypeError, IndexError):
            return
        for a in addrs:
            for pu in self.punch:
                if pu[0] == a:
                    pu[1] = now + 20.0
                    pu[2] = 0.0   # send immediately
                    break
            else:
                self.punch.append([a, now + 20.0, 0.0])  # keep punching long enough for slow STUN

    def _announce(self, now):
        self.t_ann = now
        self.broker.pub("%s/rooms/%s" % (BROKER_ROOT, self.rid), {
            "g": NET_MAGIC, "id": self.rid, "n": self.room, "h": self.pname,
            "p": len(self.peers) + 1, "m": NET_MAX_PLAYERS, "map": self.map_sig,
            "pub": list(self.pub), "lan": [lan_ip(), self.port], "ts": int(time.time()),
        }, retain=True)

    # ----- STUN: learn our public address (no server of our own needed) -----
    def _stun_start(self):
        if self.stun_started:
            return
        self.stun_started = True

        def one(h, p):
            try:
                self.stun_addrs.append((socket.gethostbyname(h), p))
            except OSError:
                pass
        for h, p in STUN_HOSTS:      # one thread per server: a slow DNS lookup blocks nobody
            threading.Thread(target=one, args=(h, p), daemon=True).start()

    def _stun_tick(self, now):
        if not (self.sock and self.stun_addrs and now >= self.t_stun):
            return
        if self.pub is None:                 # still looking for our public address: ask fast
            self.t_stun = now + 0.25
            targets = list(self.stun_addrs)
        elif self.mode == "host":
            # Keep asking while hosting: this (a) keeps the router's UDP mapping from timing out
            # (typically 30-60s idle) and (b) notices if the router gave us a different public
            # port, so the room list never advertises a dead address.
            self.t_stun = now + 8.0
            targets = self.stun_addrs[:2]
        else:
            return
        if len(self.stun_tids) > 120:
            self.stun_tids.clear()
        for a in targets:
            tid = os.urandom(12)
            self.stun_tids.add(tid)
            try:
                self.sock.sendto(struct.pack(">HHI", 1, 0, STUN_COOKIE) + tid, a)
            except OSError:
                pass

    def _stun_reply(self, data):
        mt, ln = struct.unpack_from(">HH", data, 0)
        if mt != 0x0101 or data[8:20] not in self.stun_tids:
            return
        p, end = 20, min(len(data), 20 + ln)
        xor = mapped = None
        while p + 4 <= end:
            at, al = struct.unpack_from(">HH", data, p)
            v = data[p + 4:p + 4 + al]
            if at == 0x0020 and al >= 8 and v[1] == 1:
                port = struct.unpack(">H", v[2:4])[0] ^ (STUN_COOKIE >> 16)
                ip = bytes(a ^ b for a, b in zip(v[4:8], struct.pack(">I", STUN_COOKIE)))
                xor = (".".join(str(x) for x in ip), port)
            elif at == 0x0001 and al >= 8 and v[1] == 1:
                mapped = (".".join(str(x) for x in v[4:8]), struct.unpack(">H", v[2:4])[0])
            p += 4 + al + (-al % 4)
        newpub = xor or mapped
        changed = newpub != self.pub
        self.pub = newpub
        if self.pub and self.mode == "host" and changed:
            self.t_ann = 0.0          # announce the (new) public address right away
            self.status = "Hosting '%s' - friends can join from the server browser, no port forwarding needed." % self.room

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
            if len(data) >= 20 and data[4:8] == struct.pack(">I", STUN_COOKIE):
                self._stun_reply(data)
                continue
            try:
                pkt = json.loads(data)
            except ValueError:
                continue
            self._route(pkt, addr)

    def _route(self, pkt, addr):
        if not isinstance(pkt, dict) or "p" in pkt:     # "p" = hole-punch noise
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

    # ----- relay through the brokers (used when the direct UDP hole-punch fails) -----
    def _relay_send(self, data, addr):
        self._rly_n += 1
        if self.mode == "host":
            topic = "%s/rly/%s/c/%s" % (BROKER_ROOT, self.rid, addr[1])
            who = "h"
        else:
            topic = "%s/rly/%s/h" % (BROKER_ROOT, self.join_rid)
            who = self.cid
        self.broker.pub_raw(topic, b'{"s":"%s","n":%d,"p":' % (who.encode(), self._rly_n) + data + b"}")

    def _relay_in(self, parts, j):
        if not isinstance(j, dict) or not isinstance(j.get("p"), dict):
            return
        key = (str(j.get("s")), j.get("n"))
        if key in self._rly_seen:        # same packet came in through several brokers
            return
        if len(self._rly_seen) > 4096:
            self._rly_seen.clear()
        self._rly_seen.add(key)
        if self.mode == "host" and parts[3] == "h" and parts[2] == self.rid:
            self._route(j["p"], ("relay", str(j.get("s"))[:16]))
        elif (self.mode in ("joining", "client") and parts[3] == "c" and len(parts) == 5
                and parts[2] == self.join_rid and parts[4] == self.cid):
            self._route(j["p"], ("relay", self.join_rid))

    # ----- host / join / leave -----
    def host(self, room, pname, map_sig, team=0):
        self.leave()
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
        self.players, self.peers, self.pending, self.by_addr, self.punch = {}, {}, [], {}, []
        self.next_id = 1
        self.map_sig, self.pname = map_sig, pname
        self.pub = None
        self.pub_warned = False
        self.host_t0 = time.time()
        self.t_ann = 0.0
        self.t_stun = 0.0
        self._stun_start()
        self.broker.sub("%s/sig/%s" % (BROKER_ROOT, self.rid))
        self._rly_topics = ["%s/rly/%s/h" % (BROKER_ROOT, self.rid)]
        self.broker.sub(self._rly_topics[0])
        self.status = "Hosting '%s' - finding your public address..." % room
        return True

    @staticmethod
    def room_cands(r):
        """Addresses to try for a room-browser entry."""
        out, seen = [], set()

        def add(ip, port):
            try:
                a = (str(ip), int(port))
            except (TypeError, ValueError):
                return
            if a in seen:
                return
            seen.add(a)
            out.append(a)

        if r.get("src") == "lan":
            add(r["ip"], r["port"])
        else:
            lan = r.get("lan")
            mine = lan_ip()
            # Prefer LAN when both ends look like the same private subnet
            if lan and mine.count(".") == 3 and str(lan[0]).count(".") == 3:
                if mine.rsplit(".", 1)[0] == str(lan[0]).rsplit(".", 1)[0]:
                    add(lan[0], lan[1])
                # Also try LAN address first for common private ranges (same router / VPN)
                elif str(lan[0]).startswith(("10.", "192.168.", "172.")):
                    add(lan[0], lan[1])
            add(r["ip"], r["port"])
            if lan:
                add(lan[0], lan[1])
        return out

    def join_room(self, r, pname, map_sig):
        return self.join(self.room_cands(r), r["id"] if r.get("src") == "net" else None, pname, map_sig)

    def join(self, cands, rid, pname, map_sig):
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
        self.pub = None
        self.join_rid = rid
        self.join_t0 = time.time()
        self.t_sig = 0.0
        self.t_stun = 0.0
        self._join_pub_sent = False
        self._join_punch_t = 0.0
        self._stun_start()
        self.cid = "%08x" % random.getrandbits(32)      # new for every attempt
        self._jcands = list(cands)
        self._try_t0 = time.time()
        self._join_try = 1
        self.relayed = False
        self.conn = UConn(self.mux, cands)
        self.conn.send({"t": "hello", "g": NET_MAGIC, "v": NET_PROTO, "n": pname, "map": map_sig,
                        "cid": self.cid})
        self.status = "Connecting to the server (UDP hole-punch, up to 25s)..."
        return True

    def leave(self, status=None):
        for t in self._rly_topics:
            self.broker.unsub(t)
        self._rly_topics = []
        if self.mode == "host" and self.rid:
            # kill the retained announcement, and remember the room locally so a stored copy on
            # any broker that missed the delete can never bring the closed room back
            self._dead[self.rid] = time.time()
            self.net_rooms.pop(self.rid, None)
            self._rts.pop(self.rid, None)
            self.broker.pub("%s/rooms/%s" % (BROKER_ROOT, self.rid), None, retain=True)
            self.broker.unsub("%s/sig/%s" % (BROKER_ROOT, self.rid))
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
        self.peers, self.pending, self.by_addr, self.punch, self.players = {}, [], {}, [], {}
        self.mode = None
        self.rid = ""
        self.my_id = 0
        self.pub = None
        self.join_rid = None
        if status is not None:
            self.status = status

    def close(self):
        self.leave()
        self.broker.close()
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
        self._broker_poll(now)
        if self.sock:
            self._pump(now)
            self._stun_tick(now)
        if self.mode == "host":
            self._poll_host(now, msgs)
        elif self.mode == "joining":
            self._poll_joining(now, msgs)
        elif self.mode == "client":
            self._poll_client(now, msgs)
        return msgs

    def _poll_host(self, now, msgs):
        if self.pub and now - self.t_ann >= 2.0:          # re-announce every 2s so a dead room is noticed fast
            self._announce(now)
        if not self.pub and not self.pub_warned and now - self.host_t0 > 6.0:
            self.pub_warned = True
            self.status = ("Hosting '%s' - could not find your public address (is UDP blocked?). "
                           "Only players on your network can join." % self.room)
        for pu in self.punch[:]:
            if now > pu[1]:
                self.punch.remove(pu)
            elif now >= pu[2]:
                pu[2] = now + 0.05   # fire often: keeps both NATs open during join
                try:
                    self.sock.sendto(b'{"p":1}', pu[0])
                except OSError:
                    pass
        for c in list(self.pending):
            for m in c.read():
                if m.get("t") != "hello":
                    continue
                name = str(m.get("n", "Player"))[:16] or "Player"
                cid = str(m.get("cid", ""))[:16]
                if cid and any(p.cid == cid for p in self.peers.values()):
                    self._forget(c)     # same joiner reached us over a second route: keep the first
                    c.close()
                    break
                why = None
                if m.get("v") != NET_PROTO or m.get("g") != NET_MAGIC:
                    why = "Game version mismatch."
                elif len(self.peers) + 1 >= NET_MAX_PLAYERS:
                    why = "Server is full."
                elif m.get("map") != self.map_sig:
                    why = f"Different map. The host uses '{self.map_sig}', you have '{m.get('map')}'. Load the same map first."
                if why:
                    c.send({"t": "deny", "why": why})
                    for _ in range(2):
                        c.unacked and c._raw(list(c.unacked.values())[-1][0])
                    self._forget(c)
                    c.close()
                else:
                    pid = self.next_id
                    self.next_id += 1
                    tm = self._team_for_new()
                    roster = [{"id": 0, "n": self.pname, "tm": self.my_team}] + \
                             [{"id": i, "n": p["n"], "tm": p["tm"]} for i, p in self.players.items()]
                    c.pid = pid
                    c.cid = cid
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

    def _retry_join(self, now):
        """The first hole-punch attempt is often the one that gets stuck (stale NAT mapping,
        firewall prompt, lost signal). A fresh socket + new id a few seconds later usually
        works - exactly what pressing Leave then Join did by hand. Do it automatically."""
        sock = self._open_udp()
        if sock is None:
            return
        old_sock, old_conn = self.sock, self.conn
        if old_conn:
            old_conn.close()
        if old_sock:
            try:
                old_sock.close()
            except OSError:
                pass
        self.sock = sock
        self.port = sock.getsockname()[1]
        self.pub = None
        self.stun_tids.clear()
        self.t_stun = 0.0
        self.t_sig = 0.0
        self._join_pub_sent = False
        self._join_punch_t = 0.0
        self.cid = "%08x" % random.getrandbits(32)
        self._try_t0 = now
        self._join_try += 1
        cands = self._jcands
        if self._join_try >= 3 and self.join_rid:
            # direct connection keeps failing: both NATs refuse to cooperate. Talk through the
            # brokers instead - slower, but it works through any router.
            self.relayed = True
            for t in self._rly_topics:
                self.broker.unsub(t)
            self._rly_topics = ["%s/rly/%s/c/%s" % (BROKER_ROOT, self.join_rid, self.cid)]
            self.broker.sub(self._rly_topics[0])
            cands = [("relay", self.join_rid)]
        self.conn = UConn(self.mux, cands)
        self.conn.send({"t": "hello", "g": NET_MAGIC, "v": NET_PROTO, "n": self.pname,
                        "map": self.map_sig, "cid": self.cid})

    def _poll_joining(self, now, msgs):
        c = self.conn
        # After STUN resolves, re-send hello so the host sees the correct return path
        if self.pub and not getattr(self, "_join_pub_sent", False):
            self._join_pub_sent = True
            self.t_sig = 0.0
            try:
                c.send({"t": "hello", "g": NET_MAGIC, "v": NET_PROTO, "n": self.pname,
                        "map": self.map_sig, "cid": self.cid})
            except Exception:
                pass
        if self.join_rid and now - self.t_sig >= 0.25 and (self.pub or now - self.join_t0 > 0.6):
            # tell the host (via the broker) where to punch through to us
            self.t_sig = now
            self.broker.pub("%s/sig/%s" % (BROKER_ROOT, self.join_rid), {
                "t": "punch", "cid": self.cid, "n": self.pname,
                "pub": list(self.pub) if self.pub else None, "lan": [lan_ip(), self.port]})
        # Joiner must also punch the host (classic simultaneous UDP open)
        if c and c.addrs and now - getattr(self, "_join_punch_t", 0.0) >= 0.05:
            self._join_punch_t = now
            for a in list(c.addrs):
                if a[0] == "relay":
                    continue
                try:
                    self.sock.sendto(b'{"p":1}', a)
                except OSError:
                    pass
        c.flush()
        if self.mode == "joining" and int(now - self.join_t0) != getattr(self, "_join_sec", -1):
            self._join_sec = int(now - self.join_t0)
            self.status = "Connecting to the server (%s, attempt %d, %ds / 25s)..." % (
                "via relay" if self.relayed else "direct", self._join_try, self._join_sec)
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
                self.status = f"Joined '{self.room}'." + (" (via relay - slightly higher ping)" if self.relayed else "")
                msgs.append(m)
            elif t == "deny":
                self.leave("Denied: " + str(m.get("why", "Denied.")))
                msgs.append({"t": "failed"})
                return
        if (self.mode == "joining" and not c.dead and not self.relayed
                and now - self._try_t0 > 4.0 and now - self.join_t0 < 20.0):
            self._retry_join(now)
            return
        if self.mode == "joining" and (c.dead or now - self.join_t0 > 25.0):
            why = ("Could not reach the server. Both of you should: "
                   "1) stay in the game while connecting  2) try a phone hotspot  "
                   "3) make sure neither network blocks UDP. "
                   "Symmetric NATs (some mobile/college nets) cannot peer-connect.")
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

