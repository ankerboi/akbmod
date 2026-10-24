# FAD-MOD

CS-style FPS with Source engine (L4D2 / HL2) mod support.

## Package layout

```
fadmod/
  __init__.py
  config.py       # constants, binds, weapons config, helpers
  glutil.py       # OpenGL helpers, atmosphere shaders (FX)
  world.py        # ArenaWorld, FadMapWorld (collision + draw)
  source/
    __init__.py
    vpk.py        # Valve VPK reader
    gma.py        # Garry's Mod .gma reader
    vtf.py        # KeyValues + VTF textures
    model.py      # SrcModel (MDL/VVD/VTX + animation)
    loaders.py    # view-models, player models, arms, archives
  audio.py
  net.py          # LAN + MQTT room browser + UDP peers
  entity.py       # Ent (player/bot)
  game.py         # Game state, physics, weapons, bots
  gfx.py          # 2D text/boxes, logos, RenderTarget
  menu.py         # main menu / loadout / settings UI
  render.py       # 3D scene + HUD
  settings.py     # settings.json load/save
  main.py         # entry point
```

## Run

From the directory that contains the `fadmod` package:

```bash
python run_fadmod.py
# or
python -m fadmod.main
```

Optional mods go in `mod/weapon`, `mod/player`, `mod/arms` next to the package (or in the working directory).

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

Installed automatically on first run: `numpy`, `pygame-ce`, `PyOpenGL`, and optionally `paho-mqtt`.

## Source maps (.gma / .bsp)

Open a Garry's Mod addon (`.gma`) or a Source `.bsp` in `saw_map_editor.py` (Import button, Ctrl+I, or drop the file
on the window), press F5, and load the resulting `.fadmap` in the game. The map is baked into the `.fadmap` under
`"bsp"`: a triangle mesh (drawn with the map's textures) and convex brushes (collision for players and bullets, with
real slopes and angled walls). Displacements are included. Static props (models) and moving brush entities (doors,
buttons) are not.

## Ladders

Build them in `saw_map_editor.py` with the Ladder tool (key `9` or `K`): drag a tall, thin block against a wall and press
`R` until the arrow points at the side the player stands on. F5 writes them to the `.fadmap` (`"lad"`), and the ladder is invisible in the game (build the look yourself with boxes / decals) and the game runs CS:GO-style ladder movement (`Game.ladder_tick`, tunables `LADDER_*` in `config.py`):

- Walk into a ladder holding a move key to grab it. No gravity, no friction.
- Each held key gives 156 u/s (200 x `LADDER_SCALE` 0.78) in your VIEW direction, split along the ladder: forward looks
  where you look, so W while looking up climbs, W while looking steeply down (past 45 deg) descends; A / D shuffle sideways.
- Jump leaps off at 270 u/s straight away from the ladder. Climbing past the top of the block drops you back into normal
  movement with the speed you had (you pop over the lip).
- Standing on the floor and pressing S steps you off.
