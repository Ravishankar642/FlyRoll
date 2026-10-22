"""Self-update from GitHub releases.

How it works
------------
1. ``check_latest()`` asks the GitHub API for the latest *published* release of
   ``Ravishankar642/FlyRoll`` (drafts and pre-releases are ignored by GitHub's
   ``/releases/latest`` endpoint) and compares its tag with ``__version__``.
2. ``download()`` fetches the ``FlyRoll-<tag>-windows.zip`` asset, checks the
   size and - when GitHub provides it - the SHA-256 digest, and tests the zip.
3. ``install()`` extracts it to a temp folder, writes a small ``.cmd`` script
   and starts it detached.  The script waits until FlyRoll has exited, copies
   the new files over the install folder with robocopy, restarts FlyRoll and
   deletes itself.  The logbook and user profiles live in %APPDATA%\\FlyRoll and
   are never touched.

Self-update only works for the packaged FlyRoll.exe in a folder the user can
write to.  When running from source, or installed under e.g. Program Files,
FlyRoll opens the release page instead.

The release workflow stamps the tag into ``__version__`` at build time, so
publishing a new version is just: push a tag ``vX.Y.Z``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Tuple

from . import __version__

REPO = "Ravishankar642/FlyRoll"
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
USER_AGENT = f"FlyRoll/{__version__} (+https://github.com/{REPO})"
EXE_NAME = "FlyRoll.exe"


class UpdateError(RuntimeError):
    pass


@dataclass
class Release:
    tag: str
    version: Tuple[int, ...]
    name: str
    notes: str
    page_url: str
    asset_name: str
    asset_url: str
    asset_size: int
    asset_sha256: Optional[str]


def parse_version(text: str) -> Tuple[int, ...]:
    """'v1.2.3' / '1.2' / 'v1.2.3-beta' -> (1, 2, 3); unparsable -> ()."""
    m = re.match(r"\s*v?(\d+(?:\.\d+)*)", text or "")
    if not m:
        return ()
    parts = [int(p) for p in m.group(1).split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def is_newer(candidate: str, current: Optional[str] = None) -> bool:
    a, b = parse_version(candidate), parse_version(current or __version__)
    return bool(a) and a > b


def _get(url: str, timeout: float):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout)


def check_latest(timeout: float = 10.0, api_url: str = LATEST_API) -> Optional[Release]:
    """Return the latest release if it is newer than this build, else None."""
    try:
        with _get(api_url, timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise UpdateError(f"Could not check for updates: {exc}") from exc
    tag = data.get("tag_name") or ""
    if data.get("draft") or data.get("prerelease") or not is_newer(tag):
        return None
    asset = next((a for a in data.get("assets", []) if a.get("name", "").lower().endswith("windows.zip")), None)
    digest = (asset or {}).get("digest") or ""
    return Release(
        tag=tag,
        version=parse_version(tag),
        name=data.get("name") or f"FlyRoll {tag}",
        notes=data.get("body") or "",
        page_url=data.get("html_url") or RELEASES_PAGE,
        asset_name=(asset or {}).get("name", ""),
        asset_url=(asset or {}).get("browser_download_url", ""),
        asset_size=int((asset or {}).get("size") or 0),
        asset_sha256=digest[7:].lower() if digest.startswith("sha256:") else None,
    )


# ---------------------------------------------------------------------------
def install_dir() -> Path:
    return Path(sys.executable).resolve().parent


def can_self_update() -> Tuple[bool, str]:
    """(possible, reason-if-not)."""
    if sys.platform != "win32":
        return False, "Automatic install is only available on Windows."
    if not getattr(sys, "frozen", False):
        return False, "You are running FlyRoll from source - pull the new version from GitHub instead."
    target = install_dir()
    try:
        probe = target / ".flyroll-write-test"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError:
        return False, f"FlyRoll can't write to {target} (e.g. Program Files). Download the update manually."
    return True, ""


def download(release: Release, progress: Optional[Callable[[int, int], bool]] = None,
             dest_dir: Optional[Path] = None, timeout: float = 30.0) -> Path:
    """Download the release zip. ``progress(done, total)`` may return False to cancel."""
    if not release.asset_url:
        raise UpdateError("This release has no Windows download attached.")
    dest_dir = Path(dest_dir or tempfile.mkdtemp(prefix="flyroll-update-"))
    path = dest_dir / (release.asset_name or "FlyRoll-update.zip")
    sha = hashlib.sha256()
    done = 0
    try:
        with _get(release.asset_url, timeout) as resp, open(path, "wb") as out:
            total = release.asset_size or int(resp.headers.get("Content-Length") or 0)
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                sha.update(chunk)
                done += len(chunk)
                if progress and progress(done, total) is False:
                    raise UpdateError("Download cancelled.")
    except UpdateError:
        raise
    except Exception as exc:
        raise UpdateError(f"Download failed: {exc}") from exc
    if release.asset_size and done != release.asset_size:
        raise UpdateError(f"Download incomplete ({done} of {release.asset_size} bytes).")
    if release.asset_sha256 and sha.hexdigest() != release.asset_sha256:
        raise UpdateError("Downloaded file is corrupt (SHA-256 mismatch).")
    try:
        with zipfile.ZipFile(path) as zf:
            bad = zf.testzip()
    except zipfile.BadZipFile as exc:
        raise UpdateError("Downloaded file is not a valid zip.") from exc
    if bad:
        raise UpdateError(f"Downloaded zip is damaged ({bad}).")
    return path


def extract(zip_path: Path) -> Path:
    """Extract and return the folder that contains FlyRoll.exe."""
    target = Path(tempfile.mkdtemp(prefix="flyroll-new-"))
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():           # refuse path traversal
            resolved = (target / member).resolve()
            if not str(resolved).startswith(str(target.resolve())):
                raise UpdateError("Update archive contains unsafe paths.")
        zf.extractall(target)
    for exe in target.rglob(EXE_NAME):
        return exe.parent
    raise UpdateError(f"{EXE_NAME} not found in the update.")


def write_update_script(new_dir: Path, target_dir: Path, pid: int, cleanup: Path) -> Path:
    script = Path(tempfile.gettempdir()) / f"flyroll-update-{pid}.cmd"
    script.write_text(f"""@echo off
chcp 65001 >nul
title FlyRoll update
echo Updating FlyRoll - please wait...
:wait
tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul
if not errorlevel 1 (
    timeout /t 1 /nobreak >nul
    goto wait
)
robocopy "{new_dir}" "{target_dir}" /E /R:10 /W:1 /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 (
    echo.
    echo Update failed - could not copy the new files into "{target_dir}".
    echo Download it manually from {RELEASES_PAGE}
    pause
    exit /b 1
)
start "" "{target_dir / EXE_NAME}"
rmdir /s /q "{cleanup}" >nul 2>&1
(goto) 2>nul & del "%~f0"
""", encoding="utf-8")
    return script


def install(zip_path: Path) -> None:
    """Start the detached updater; the caller must quit FlyRoll right after."""
    ok, reason = can_self_update()
    if not ok:
        raise UpdateError(reason)
    new_dir = extract(zip_path)
    cleanup = next(p for p in (new_dir, *new_dir.parents) if p.name.startswith("flyroll-new-"))
    script = write_update_script(new_dir, install_dir(), os.getpid(), cleanup)
    # own small console window, so progress / a copy error is visible after FlyRoll has quit
    CREATE_NEW_CONSOLE = 0x00000010
    subprocess.Popen(["cmd.exe", "/c", str(script)], creationflags=CREATE_NEW_CONSOLE, close_fds=True,
                     cwd=tempfile.gettempdir())
    if zip_path.parent.name.startswith("flyroll-update-"):
        shutil.rmtree(zip_path.parent, ignore_errors=True)
