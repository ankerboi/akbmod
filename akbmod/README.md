# AKB-MOD

Source engine (L4D2 / HL2) mod support.

## Package layout

```
akbmod/
  __init__.py
  config.py       # constants, binds, weapons config, helpers
  glutil.py       # OpenGL helpers, atmosphere shaders (FX)
  world.py        # ArenaWorld, AkbMapWorld (collision + draw)
  source/
    __init__.py
    vpk.py        # Valve VPK reader
    gma.py        # Garry's Mod .gma reader
    vtf.py        # KeyValues + VTF textures
    model.py      # SrcModel (MDL/VVD/VTX + animation)
    loaders.py    # view-models, player models, arms, archives
  audio.py
  net.py          # LAN discovery + direct UDP peers (offline + LAN only)
  entity.py       # Ent (player/bot)
  game.py         # Game state, physics, weapons, bots
  gfx.py          # 2D text/boxes, logos, RenderTarget
  menu.py         # main menu / loadout / settings UI
  render.py       # 3D scene + HUD
  settings.py     # settings.json load/save
  main.py         # entry point
```

## Run

From the directory that contains the `akbmod` package:

```bash
python run_akbmod.py
# or
python -m akbmod.main
```

Optional mods go in `assets/weapon`, `assets/player`, `assets/arms`, `assets/sound`, `assets/image` (and `assets/maps`) inside the package, next to it, or in the working directory. The old `mod/...` folders still work. Singular and plural names both match (`weapon` = `weapons`).

## Garry's Mod addons (.gma) as weapon / player / arms mods

Drop a `.gma` into `mod/weapon`, `mod/player` or `mod/arms`, drag it onto the window, or use Add files. It is read in
place (nothing is unpacked), like a `.vpk`, and one addon can give weapons, players and arms together:

- Weapons: `v_*.mdl`, `c_*.mdl`, and every `SWEP.ViewModel` named in the addon's `lua/weapons/*.lua`. `c_` models have
  no hands: pick an arms model in Skins (a `c_arms*.mdl` inside the addon is found automatically). Sounds inside the
  addon are used for the weapon they belong to. Weapon stats stay the game's own.
- Players: `models/player/...` and anything the addon registers with `player_manager.AddValidModel`.
- Workshop legacy downloads (`<id>_legacy.bin`) work too: a `.bin` is checked by its contents and treated as a `.gma` if it is one (other `.bin` files are ignored).
- Workshop downloads (LZMA-compressed `.gma`) are unpacked once into the temp folder. A `.gma` inside a `.zip` / `.7z` /
  `.rar` works too. A `.gma` that is a map goes through `saw_map_editor.py` (see below).

## Dependencies

Installed automatically on first run: `numpy`, `pygame-ce`, `PyOpenGL`.

## Play modes

Offline and LAN only (no internet servers, no brokers, no hole punching). Host a LAN match in Play > Create match > Mode: LAN; players on the same network see it in the server list. Over the internet, use a virtual LAN such as Radmin VPN and type the host's IP in Join by IP.

## Maps load themselves

On start the last map you used loads (or the first `.akbmap` found in the maps folders), and Start match loads one automatically if none is loaded. Joining a LAN game loads the host's map by itself when you have it.

## Source maps (.gma / .bsp)

Open a Garry's Mod addon (`.gma`) or a Source `.bsp` in `saw_map_editor.py` (Import button, Ctrl+I, or drop the file
on the window), press F5, and load the resulting `.akbmap` in the game. The map is baked into the `.akbmap` under
`"bsp"`: a triangle mesh (drawn with the map's textures) and convex brushes (collision for players and bullets, with
real slopes and angled walls). Displacements are included. Static props (models) and moving brush entities (doors,
buttons) are not.

## Ladders

Build them in `saw_map_editor.py` with the Ladder tool (key `9` or `K`): drag a tall, thin block against a wall and press
`R` until the arrow points at the side the player stands on. F5 writes them to the `.akbmap` (`"lad"`), and the ladder is invisible in the game (build the look yourself with boxes / decals) and the game runs CS:GO-style ladder movement (`Game.ladder_tick`, tunables `LADDER_*` in `config.py`):

- Walk into a ladder holding a move key to grab it. No gravity, no friction.
- Each held key gives 156 u/s (200 x `LADDER_SCALE` 0.78) in your VIEW direction, split along the ladder: forward looks
  where you look, so W while looking up climbs, W while looking steeply down (past 45 deg) descends; A / D shuffle sideways.
- Jump leaps off at 270 u/s straight away from the ladder. Climbing past the top of the block drops you back into normal
  movement with the speed you had (you pop over the lip).
- Standing on the floor and pressing S steps you off.

## sMove score system

Ported from the sMove gamemode. Tricks add to a **held** pool; killing someone banks the held points plus 300.

| Trick | Points |
|---|---|
| Bunnyhop (2nd+ chained hop above 250 u/s) | 5 |
| Pixelsurf (per tick, above 150 u/s) | 1 |
| Jumpbug | 20 |
| Edgebug | 200 |
| Kill | 300 (+ held) |

Every trick restarts a 3 second timer; if it runs out (or you die) the held points are lost. Tunables are the `SC_*`
constants in `config.py`. The HUD (`draw_smove_hud` in `render.py`) uses the Stratum font and kill sound shipped in
`assets/`. The velocity box (`speed (speed at last bhop)`) is part of the Score toggle in settings (Score: OFF turns the whole system off).

## Map selection
Play > Create match > **Load map** opens a map list with a picture for each map (the arena plus every .akbmap
in `mod/maps`; maps you load once are kept there). Click a map and press Load map, or double-click it.
Browse file... still lets you pick a .akbmap from anywhere. The picture comes from the map builder (Preview button).
