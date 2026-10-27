![akb](https://i.ibb.co/99LDCJjC/images-steamusercontent.jpg)

## Run

```
python run_akbmod.py     # game
python saw_editor.py     # map builder
```

Python 3.10+. Packages install on first run.

## Features

- Up to 6 players on LAN. Bots only via the console (`` ` ``).
- Smove score: bhop 5, pixel surf 1/tick, jumpbug 20, edgebug 200, kill 300 + held points.
- Pixel surf works only inside pixel surf zones.
- Keys (remappable): WASD, Space, Ctrl crouch, R reload, 1/2 weapons, Z/X/C jumpbug/edgebug/pixel surf, V swap hand, B third person.

## Mods

Put weapon mods in their own folder under `akbmod/assets/weapon/`:

`ak-47` · `awp` · `knife` · `pistol` · `shotgun`

Loose files in `weapon/` apply to every weapon. Other folders in `akbmod/assets/`: `player`, `arms`, `sound`, `image`. Pick mods in the **Skins** tab.

Supports Source models (`.mdl`, `.vpk`) and Garry's Mod `.gma` addons. CS 1.6 models don't work.

## Maps

1. In `saw_editor.py`, draw boxes (`B`) and place spawns (`1` BUM, `2` FAD).
2. `Ctrl+S` saves the project (`.akbproj`), `F5` compiles it to `.akbmap`.
3. Put the `.akbmap` in `akbmod/assets/map`.

Tools: `3` slope, `4` cylinder, `7` decal, `8` pixel surf zone, `9` ladder, `Ctrl+I` import `.gma`/`.bsp`/`.vpk`. `F1` = help.

## Play with Radmin VPN

1. Everyone joins the same Radmin VPN network and has the same `.akbmap`.
2. **Host:** Play > Create match > Mode: LAN > Start match. Allow the game through the firewall (UDP) and share your Radmin IP (`26.x.x.x`).
3. **Friends:** Play > type the IP in **Join by IP** > Join.
