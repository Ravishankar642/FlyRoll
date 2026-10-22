"""Launcher used by PyInstaller and for double-click starts: python FlyRoll.py [--demo]"""
import sys

from flyroll.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
