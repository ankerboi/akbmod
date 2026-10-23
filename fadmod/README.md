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

## Dependencies

Installed automatically on first run: `numpy`, `pygame-ce`, `PyOpenGL`, and optionally `paho-mqtt`.
