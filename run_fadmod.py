#!/usr/bin/env python3
"""Run FAD-MOD."""
import sys
sys.dont_write_bytecode = True      # no __pycache__ folders next to the game
from fadmod.main import main

if __name__ == "__main__":
    main()
