# AKB-MOD

First-person team deathmatch (BUM vs FAD) with Source weapon/player mods, CS:GO-style movement, a trick score system and a 3D map builder. Offline or LAN only.

## Run

```
python run_akbmod.py        # the game
python saw_editor.py        # the map builder
```

Missing packages (`numpy`, `pygame-ce`, `PyOpenGL`) install on first run. Needs Python 3.10+.

## Features

- Team deathmatch, up to 6 players on LAN. Offline bots only via the dev console (`` ` ``).
- Maps load by themselves (last used, else the first `.akbmap` in the map folders).
- sMove score: bhop 5, pixel surf 1/tick, jumpbug 20, edgebug 200, kill 300 + held points.
- Pixel surf only inside pixel surf zones; CS:GO-style ladders.
- Keys (remappable): WASD, Space jump, Ctrl crouch, R reload, 1/2 weapons, Z/X/C jumpbug/edgebug/pixel surf, V swap hand, B third person.

## Mods

Put files in `akbmod/assets/` (or `mod/`): `weapon`, `player`, `arms`, `sound`, `image`. Or drag them onto the game window. Pick them in the **Skins** tab.

Supported: Source models (`.mdl`, `.vpk`) and Garry's Mod addons (`.gma`, also inside `.zip`/`.7z`/`.rar`). GoldSrc / CS 1.6 models do not work.

## Maps

1. Run `python saw_editor.py`. Draw boxes (`B`), place spawns (`1` BUM, `2` FAD), drop images on faces to texture them.
2. `Ctrl+S` saves the project (`.akbproj`). `F5` compiles it to a `.akbmap`.
3. Put the `.akbmap` in `akbmod/assets/map`.

Other tools: `3` slope, `4` cylinder, `7` decal, `8` pixel surf zone, `9` ladder, `Ctrl+I` import a `.gma` / `.bsp` / `.vpk` map. `F1` shows all controls.

## Play online with Radmin VPN

1. Everyone installs Radmin VPN. One person creates a network, the others join it.
2. Everyone has the same `.akbmap` in `akbmod/assets/map`.
3. Host: **Play > Create match > Mode: LAN > Start match**. Allow the game through the firewall (UDP). Send friends your Radmin IP (starts with `26.`).
4. Friends: **Play**, type the IP in **Join by IP** (`26.x.x.x`, or `26.x.x.x:27778`), press **Join**.

## Problems

- **No map found:** put a `.akbmap` in `akbmod/assets/map` (a `.gma`/`.bsp` must be compiled with F5 first).
- **Different map:** use the host's exact `.akbmap`.
- **Could not reach the host:** same Radmin network? IP right? Firewall allows UDP?
