"""A stand-in for GitHub's "latest release" API, serving this checkout as the
release. Used by the updater tests and by CI to try the installers.

    python3 tests/fake_github.py PORT VERSION
"""
from __future__ import annotations

import http.server
import io
import json
import sys
import threading
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INCLUDE = ("src", "extension", "scripts", "tests", "VERSION", "requirements.txt", "README.md", "install.sh", "install.ps1")


def make_zip(version: str, overrides: dict[str, str] | None = None) -> bytes:
    """A GitHub-style source zip (one top folder). `overrides` replaces or adds
    files; a value of None leaves that file out."""
    files: dict[str, bytes] = {}
    for name in INCLUDE:
        path = ROOT / name
        for f in ([path] if path.is_file() else path.rglob("*") if path.is_dir() else []):
            if f.is_file() and "__pycache__" not in f.parts and ".moodle_session" not in f.parts:
                files[f.relative_to(ROOT).as_posix()] = f.read_bytes()
    files["VERSION"] = f"{version}\n".encode()
    for name, content in (overrides or {}).items():
        if content is None:
            files.pop(name, None)
        else:
            files[name] = content.encode("utf-8")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(f"owner-repo-abc123/{name}", content)
    return buffer.getvalue()


class FakeGitHub:
    def __init__(self, version: str, archive: bytes | None = None, port: int = 0):
        self.version, self.archive = version, archive or make_zip(version)
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path.endswith("/releases/latest"):
                    body = json.dumps({"tag_name": f"v{fake.version}", "body": "- a change",
                                       "zipball_url": f"{fake.url}/zip"}).encode()
                elif self.path == "/zip":
                    body = fake.archive
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


if __name__ == "__main__":
    fake = FakeGitHub(sys.argv[2], port=int(sys.argv[1]))
    print(f"serving version {sys.argv[2]} at {fake.url}", flush=True)
    threading.Event().wait()
