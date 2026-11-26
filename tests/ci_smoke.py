"""CI only (it installs a shortcut and a daily job on the machine): run the real
installer for this OS against a stand-in GitHub, start the app, run the daily
job, then update it from inside the app and check it restarts on the new version.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from fake_github import ROOT, FakeGitHub, make_zip  # noqa: E402

WINDOWS = sys.platform.startswith("win")
PORT = 8791
APP = f"http://127.0.0.1:{PORT}"


def step(text: str) -> None:
    print(f"\n=== {text}", flush=True)


def powershell(script: str) -> str:
    return subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True, capture_output=True, text=True).stdout.strip()


def request(path: str, body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(APP + path, data=data, headers={"Content-Type": "application/json"} if body is not None else {})
    with urllib.request.urlopen(req, timeout=300) as response:
        return json.loads(response.read())


def wait_for(version: str, seconds: int = 60) -> None:
    last = None
    for _ in range(seconds):
        try:
            last = request("/api/status")
            if last["version"] == version and not last["restart_needed"]:
                return
        except OSError as e:
            last = str(e)
        time.sleep(1)
    raise AssertionError(f"app never reported version {version}: {last}")


def main() -> None:
    if not os.environ.get("CI"):
        sys.exit("This installs a shortcut and a daily job; it only runs in CI.")
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    newer = "99.0.0"
    destination = Path(tempfile.mkdtemp()) / "LMS Sync app"  # a space in the path, on purpose
    fake = FakeGitHub(version)
    env = {**os.environ, "LMS_SYNC_UPDATE_API": fake.url, "LMS_SYNC_DIR": str(destination), "LMS_SYNC_NO_START": "1"}

    step("install")
    if WINDOWS:
        command = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                   f"Get-Content -Raw '{ROOT / 'install.ps1'}' | Invoke-Expression"]
    else:
        command = ["bash", str(ROOT / "install.sh")]
    subprocess.run(command, env=env, check=True)
    python = destination / ".venv" / ("Scripts/python.exe" if WINDOWS else "bin/python3")
    assert python.exists() and (destination / "src" / "cli.py").exists(), "files missing after install"

    step("desktop shortcut and daily schedule")
    if WINDOWS:
        shortcut = Path(powershell("Join-Path ([Environment]::GetFolderPath('Desktop')) 'LMS Sync.lnk'"))
        assert shortcut.exists(), f"no shortcut at {shortcut}"
        assert "LMS Sync daily" in powershell("(Get-ScheduledTask -TaskName 'LMS Sync daily').TaskName")
    else:
        assert (Path.home() / "Desktop" / "LMS Sync.app").exists(), "no Desktop app"
        assert (Path.home() / "Library" / "LaunchAgents" / "com.lmssync.daily.plist").exists(), "no LaunchAgent"

    step("start the app")
    log = destination / "app.log"
    with open(log, "wb") as out:
        subprocess.Popen([str(python), str(destination / "src" / "cli.py"), "ui", "--no-open", "--port", str(PORT)],
                         cwd=destination / "src", env=env, stdout=out, stderr=out)
    try:
        wait_for(version)
        status = request("/api/status")
        assert status["state"] == "missing", status  # a fresh install isn't logged in
        assert status["os"] == ("windows" if WINDOWS else "mac"), status

        step("turn a pdf2htmlEX web page back into a PDF (real browser)")
        sample = ('<!DOCTYPE html><!-- Created by pdf2htmlEX --><html><head><style>.pf{width:600px;height:800px;'
                  'overflow:hidden;position:relative}@media print{.pf{page-break-after:always}}</style></head><body>'
                  '<div id="page-container"><div class="pf">Page one</div><div class="pf">Page two</div></div></body></html>')
        code = ("import sys, re, browser; pdf = browser.html_pages_to_pdf(sys.stdin.buffer.read()); "
                "assert pdf.startswith(b'%PDF'); pages = len(re.findall(rb'/Type\\s*/Page(?![s])', pdf)); "
                "print('pages', pages); assert pages == 2, pages")
        subprocess.run([str(python), "-c", code], cwd=destination / "src", env=env, input=sample.encode(), check=True, timeout=180)

        step("daily job")
        subprocess.run([str(python), str(destination / "src" / "cli.py"), "daily"], env=env, check=True, timeout=300)

        step("update from inside the app, which then restarts itself")
        fake.version, fake.archive = newer, make_zip(newer)
        (destination / "data").mkdir(exist_ok=True)
        (destination / "data" / "keep.txt").write_text("mine", encoding="utf-8")
        assert request("/api/update/check", {})["available"] is True
        assert request("/api/update/install", {})["updated_to"] == newer
        wait_for(newer, 120)
        assert (destination / "data" / "keep.txt").read_text(encoding="utf-8") == "mine"
        print("updated and restarted OK")
    finally:
        for name in ("app.log", "logs/ui.log", "logs/daily.log"):
            path = destination / name
            if path.exists():
                print(f"--- {name}\n{path.read_text(encoding='utf-8', errors='replace')[-3000:]}")
        step("stop and uninstall")
        if WINDOWS:
            powershell("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*LMS Sync app*' -and "
                       "$_.Name -like 'python*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
        else:
            subprocess.run(["pkill", "-f", "LMS Sync app/src/cli.py"], check=False)
        subprocess.run([str(python), str(destination / "src" / "cli.py"), "uninstall-integration"], env=env, check=False)
        fake.close()


if __name__ == "__main__":
    main()
