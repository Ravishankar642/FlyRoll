"""FlyRoll entry point.

    python -m flyroll                 connect to MSFS 2020 / 2024 via SimConnect
    python -m flyroll --demo fenix    try it without the simulator (fenix | a350 | a380 | c172)
"""
from __future__ import annotations

import argparse
import sys


def main(argv=None) -> int:
    from . import APP_NAME, __version__

    ap = argparse.ArgumentParser(prog="FlyRoll", description=f"{APP_NAME} {__version__} - MSFS flight recorder")
    ap.add_argument("--demo", nargs="?", const="fenix", choices=["fenix", "a350", "a380", "c172"],
                    help="use the built-in demo simulator instead of MSFS")
    ap.add_argument("--rate", type=float, default=30.0, help="recording sample rate in Hz (default 30)")
    ap.add_argument("--dll", help="full path to SimConnect.dll")
    args = ap.parse_args(argv)

    from PySide6.QtWidgets import QApplication

    from .core.logbook import Logbook
    from .core.profiles import ProfileStore, user_data_dir
    from .core.session import Session
    from .ui import theme
    from .ui.main_window import Bridge, MainWindow

    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    app.setStyleSheet(theme.QSS)

    if args.demo:
        from .sim.mock import MockBackend
        kind = args.demo

        def factory():
            return MockBackend(kind)
        logbook_path = user_data_dir() / "demo-logbook.sqlite"
    else:
        from .sim.native import NativeBackend
        dll = args.dll

        def factory():
            return NativeBackend(dll)
        logbook_path = user_data_dir() / "logbook.sqlite"

    profiles = ProfileStore()
    logbook = Logbook(logbook_path)
    bridge = Bridge()
    session = Session(factory, profiles, bridge, sample_rate=args.rate)
    window = MainWindow(session, logbook, profiles, bridge)
    if args.demo:
        window.setWindowTitle(window.windowTitle() + "  —  DEMO (no simulator)")
    window.show()
    session.start()
    session.connect()
    code = app.exec()
    logbook.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
