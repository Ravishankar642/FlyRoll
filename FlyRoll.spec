# PyInstaller spec - build with:  pyinstaller --noconfirm FlyRoll.spec
import importlib.util
from pathlib import Path

root = Path(SPECPATH)
binaries = []
# Prefer a SimConnect.dll you copied next to this file (e.g. from the MSFS 2024 SDK),
# otherwise take the one shipped with the "SimConnect" pip package.
dll = root / "SimConnect.dll"
if not dll.is_file():
    spec = importlib.util.find_spec("SimConnect")
    if spec and spec.origin:
        dll = Path(spec.origin).parent / "SimConnect.dll"
if dll.is_file():
    binaries.append((str(dll), "."))
else:
    print("WARNING: SimConnect.dll not bundled - copy it next to FlyRoll.exe")

a = Analysis(
    [str(root / "FlyRoll.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=[(str(root / "flyroll" / "profiles"), "profiles")],
    hiddenimports=["flyroll.sim.native", "flyroll.sim.mock"],
    excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.Qt3DCore"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="FlyRoll", console=False,
          icon=str(root / "assets" / "flyroll.ico") if (root / "assets" / "flyroll.ico").exists() else None)
coll = COLLECT(exe, a.binaries, a.datas, name="FlyRoll")
