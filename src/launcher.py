"""What the desktop shortcut runs (macOS and Windows): make sure the app is
running in the background, updating first if allowed, then open it in the
browser."""
from __future__ import annotations

import sys
import time
import webbrowser
from pathlib import Path

import requests

import desktop
import updater

SRC = Path(__file__).parent
PORT = 8765
URL = f"http://127.0.0.1:{PORT}/"


def running() -> bool:
    try:
        return requests.get(URL + "api/ping", timeout=1).ok
    except requests.RequestException:
        return False


def main() -> None:
    if not running():
        updater.install_if_automatic()  # nothing is running yet: the safest moment
        desktop.spawn_detached(
            [desktop.background_python(), str(SRC / "cli.py"), "ui", "--no-open"], SRC, SRC.parent / "logs" / "ui.log")
        for _ in range(40):
            time.sleep(0.5)
            if running():
                break
    webbrowser.open(URL)


if __name__ == "__main__":
    if sys.stdout is None:  # started with pythonw (no console)
        log = SRC.parent / "logs"
        log.mkdir(exist_ok=True)
        sys.stdout = sys.stderr = open(log / "launcher.log", "a", encoding="utf-8")
    main()
