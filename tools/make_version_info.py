"""Write a Windows VERSIONINFO resource file for PyInstaller.

Executables without version/company metadata look anonymous to antivirus
machine-learning models (a common cause of false positives for PyInstaller
apps), so the build embeds proper metadata.

    python tools/make_version_info.py            -> uses flyroll.__version__
    python tools/make_version_info.py 1.2.0
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    if len(sys.argv) > 1:
        version = sys.argv[1].lstrip("v")
    else:
        text = (ROOT / "flyroll" / "__init__.py").read_text(encoding="utf-8")
        version = re.search(r'__version__ = "([^"]+)"', text).group(1)
    nums = [int(x) for x in re.findall(r"\d+", version)[:4]]
    nums += [0] * (4 - len(nums))
    tup = ", ".join(map(str, nums))
    dotted = ".".join(map(str, nums))
    out = ROOT / "build" / "version_info.txt"
    out.parent.mkdir(exist_ok=True)
    out.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({tup}), prodvers=({tup}), mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'FlyRoll (open source)'),
      StringStruct('FileDescription', 'FlyRoll - flight recorder and replay for Microsoft Flight Simulator'),
      StringStruct('FileVersion', '{dotted}'),
      StringStruct('InternalName', 'FlyRoll'),
      StringStruct('LegalCopyright', 'Copyright (c) 2026 FlyRoll contributors. MIT License.'),
      StringStruct('OriginalFilename', 'FlyRoll.exe'),
      StringStruct('ProductName', 'FlyRoll'),
      StringStruct('ProductVersion', '{version}'),
      StringStruct('Comments', 'https://github.com/Ravishankar642/FlyRoll')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""", encoding="utf-8")
    print(f"wrote {out} ({version})")


if __name__ == "__main__":
    main()
