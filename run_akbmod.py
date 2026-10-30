#!/usr/bin/env python3
"""Run AKB-MOD."""
import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True      # no __pycache__ folders next to the game

# Spacewar: steam_appid.txt must exist in the working directory before SteamAPI_Init
_root = Path(__file__).resolve().parent
try:
    os.chdir(_root)
except OSError:
    pass
(_root / "steam_appid.txt").write_text("480\n")
(_root / "akbmod" / "steam_appid.txt").write_text("480\n")

from akbmod.main import main

if __name__ == "__main__":
    main()
