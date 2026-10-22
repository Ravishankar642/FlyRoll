"""Updater tests against a local HTTP server pretending to be GitHub."""
import hashlib
import http.server
import io
import json
import sys
import threading
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flyroll import updater  # noqa: E402


def make_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("FlyRoll/FlyRoll.exe", b"MZ fake exe")
        zf.writestr("FlyRoll/_internal/profiles/fenix-a32x.json", b"{}")
    return buf.getvalue()


@pytest.fixture
def github():
    payload = make_zip()
    state = {"tag": "v9.9.0", "digest": "sha256:" + hashlib.sha256(payload).hexdigest(), "zip": payload}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            base = f"http://127.0.0.1:{self.server.server_port}"
            if self.path == "/latest":
                body = json.dumps({"tag_name": state["tag"], "name": "FlyRoll " + state["tag"],
                                   "body": "## What's new\n- stuff", "html_url": base + "/page",
                                   "draft": False, "prerelease": False,
                                   "assets": [{"name": f"FlyRoll-{state['tag']}-windows.zip",
                                               "browser_download_url": base + "/zip",
                                               "size": len(state["zip"]), "digest": state["digest"]}]}).encode()
            elif self.path == "/zip":
                body = state["zip"]
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    state["api"] = f"http://127.0.0.1:{srv.server_port}/latest"
    yield state
    srv.shutdown()


def test_versions():
    assert updater.parse_version("v1.2.3") == (1, 2, 3)
    assert updater.parse_version("1.2.0") == updater.parse_version("v1.2")
    assert updater.is_newer("v1.10.0", "1.9.9")
    assert not updater.is_newer("v1.1.0", "1.1.0")
    assert not updater.is_newer("garbage", "1.0.0")


def test_check_download_extract(github, monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "__version__", "1.1.0")
    rel = updater.check_latest(api_url=github["api"])
    assert rel and rel.tag == "v9.9.0" and rel.asset_sha256
    seen = []
    path = updater.download(rel, lambda d, t: seen.append((d, t)) or True, dest_dir=tmp_path)
    assert seen[-1][0] == seen[-1][1] == rel.asset_size
    exe_dir = updater.extract(path)
    assert (exe_dir / "FlyRoll.exe").read_bytes() == b"MZ fake exe"


def test_no_update_when_current(github, monkeypatch):
    monkeypatch.setattr(updater, "is_newer", lambda tag, cur=None: False)
    assert updater.check_latest(api_url=github["api"]) is None


def test_corrupt_download_rejected(github, tmp_path):
    rel = updater.check_latest(api_url=github["api"])
    github["zip"] = github["zip"][:-10] + b"x" * 10      # same size, different bytes
    with pytest.raises(updater.UpdateError, match="SHA-256"):
        updater.download(rel, dest_dir=tmp_path)


def test_cancel(github, tmp_path):
    rel = updater.check_latest(api_url=github["api"])
    with pytest.raises(updater.UpdateError, match="cancelled"):
        updater.download(rel, lambda d, t: False, dest_dir=tmp_path)


def test_unsafe_zip_rejected(tmp_path):
    p = tmp_path / "evil.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("../../evil.txt", b"x")
    with pytest.raises(updater.UpdateError, match="unsafe"):
        updater.extract(p)


def test_update_script(tmp_path):
    script = updater.write_update_script(tmp_path / "new", tmp_path / "app", 4242, tmp_path / "new")
    text = script.read_text()
    assert 'PID eq 4242' in text and "robocopy" in text and "FlyRoll.exe" in text
    script.unlink()


def test_self_update_not_from_source():
    ok, reason = updater.can_self_update()
    assert not ok and reason
