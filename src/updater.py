"""Updates an installed copy of LMS Sync from the project's GitHub releases.

Only published *releases* are installed (never whatever is on a branch), over
HTTPS, from one fixed repository. Order: download → check it is complete and
starts → install new dependencies → copy the files over, with a backup restored
if copying fails. The user's data (data/, .env, .venv, logs) is never touched.

Three kinds of copy:
  installed    put there by install.sh / install.ps1 — updates itself
  development  a git checkout — never overwritten; update with git
  plugin       the Claude plugin — updated by the plugin system
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import requests

import desktop
import moodle_client
import state

ROOT = moodle_client.PROJECT_ROOT
REPO = os.environ.get("LMS_SYNC_UPDATE_REPO", "Pulkit-123/lms-sync-app")
API = os.environ.get("LMS_SYNC_UPDATE_API", "https://api.github.com")
# What an update replaces. Anything else in the folder is the user's.
MANAGED = ("src", "extension", "scripts", "tests", "VERSION", "requirements.txt", "README.md", "LICENSE",
           "install.sh", "install.ps1")
REQUIRED = ("VERSION", "requirements.txt", "src/cli.py", "src/server.py", "src/updater.py", "src/ui/index.html")
CHECK_EVERY_S = 6 * 3600
_HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": "lms-sync-updater"}


class UpdateError(RuntimeError):
    pass


def current_version(root: Path | None = None) -> str:
    try:
        return ((root or ROOT) / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "0"


def _numbers(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in version.lstrip("vV").split("."))
    except ValueError:
        return (0,)


def is_newer(candidate: str, than: str) -> bool:
    return _numbers(candidate) > _numbers(than)


def mode() -> str:
    if os.environ.get("CLAUDE_PLUGIN_DATA"):
        return "plugin"
    return "development" if (ROOT / ".git").exists() else "installed"


# ---- settings and the cached check -----------------------------------------

def _load(name: str) -> dict:
    try:
        return json.loads((state.DATA_DIR / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(name: str, data: dict) -> None:
    state.DATA_DIR.mkdir(parents=True, exist_ok=True)
    (state.DATA_DIR / name).write_text(json.dumps(data), encoding="utf-8")


def auto_enabled() -> bool:
    return bool(_load("settings.json").get("auto_update", True))


def set_auto(value: bool) -> None:
    _save("settings.json", {**_load("settings.json"), "auto_update": bool(value)})


def latest_release() -> dict | None:
    """{version, notes, zip} of the newest published release, or None if there
    isn't one yet."""
    response = requests.get(f"{API}/repos/{REPO}/releases/latest", headers=_HEADERS, timeout=15)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    data = response.json()
    return {"version": data["tag_name"].lstrip("vV"), "notes": (data.get("body") or "")[:4000], "zip": data["zipball_url"]}


def check(force: bool = False) -> dict:
    """What the app shows. Cached for a few hours (GitHub rate-limits anonymous
    callers); never raises."""
    cached = _load("update.json")
    if force or cached.get("repo") != REPO or time.time() - cached.get("checked", 0) > CHECK_EVERY_S:
        try:
            release = latest_release()
            cached = {"repo": REPO, "checked": time.time(), "latest": release, "error": ""}
        except (requests.RequestException, ValueError, KeyError) as e:
            cached = {**cached, "repo": REPO, "checked": time.time(), "error": f"Couldn't check for updates: {e}"}
        _save("update.json", cached)
    release = cached.get("latest")
    now = current_version()
    return {
        "current": now,
        "latest": release["version"] if release else now,
        "available": bool(release) and is_newer(release["version"], now),
        "notes": release["notes"] if release else "",
        "error": cached.get("error", ""),
        "mode": mode(),
        "auto": auto_enabled(),
    }


# ---- installing -----------------------------------------------------------

def _extract(archive: bytes, into: Path) -> Path:
    """Unpack a GitHub source zip (one top folder) safely; returns that folder."""
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        for name in z.namelist():
            target = (into / name).resolve()
            if into.resolve() not in target.parents and target != into.resolve():
                raise UpdateError("The download contains an unsafe path.")
        z.extractall(into)
    tops = [p for p in into.iterdir() if p.is_dir()]
    if len(tops) != 1:
        raise UpdateError("The download doesn't look like LMS Sync.")
    return tops[0]


def _verify(new: Path, version: str) -> None:
    missing = [name for name in REQUIRED if not (new / name).is_file()]
    if missing:
        raise UpdateError(f"The download is incomplete (missing {', '.join(missing)}).")
    if current_version(new) != version:
        raise UpdateError("The download's version doesn't match the release.")


def _install_requirements(new: Path) -> None:
    wanted = (new / "requirements.txt").read_text(encoding="utf-8")
    have = (ROOT / "requirements.txt").read_text(encoding="utf-8") if (ROOT / "requirements.txt").exists() else ""
    if wanted.split() == have.split():
        return
    run = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-q", "-r", str(new / "requirements.txt")], capture_output=True, text=True)
    if run.returncode != 0:
        raise UpdateError(f"Couldn't install the new version's dependencies: {run.stderr.strip()[-300:]}")


def _smoke_test(new: Path) -> None:
    """The new code must at least import before it replaces the working one."""
    with tempfile.TemporaryDirectory() as data:
        env = {**os.environ, "LMS_SYNC_DATA": data, "PYTHONDONTWRITEBYTECODE": "1"}
        run = subprocess.run([sys.executable, "-c", "import cli, server, updater"], cwd=new / "src", env=env,
                             capture_output=True, text=True, timeout=120)
    if run.returncode != 0:
        raise UpdateError(f"The new version doesn't start: {run.stderr.strip()[-300:]}")


def _mirror(source: Path, target: Path) -> None:
    """Make `target` match `source`, file by file (a folder itself is never
    renamed: Chrome holds the extension folder open on Windows)."""
    if source.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        return
    wanted = {p.relative_to(source) for p in source.rglob("*") if p.is_file()}
    for relative in wanted:
        _mirror(source / relative, target / relative)
    for existing in list(target.rglob("*")):
        if existing.is_file() and existing.relative_to(target) not in wanted and "__pycache__" not in existing.parts:
            existing.unlink()


def _apply(new: Path, backup: Path) -> None:
    shutil.rmtree(backup, ignore_errors=True)
    backup.mkdir(parents=True)
    present = [name for name in MANAGED if (new / name).exists()]
    for name in present:
        if (ROOT / name).exists():
            _mirror(ROOT / name, backup / name)
    try:
        for name in present:
            _mirror(new / name, ROOT / name)
    except OSError as e:
        for name in present:
            if (backup / name).exists():
                _mirror(backup / name, ROOT / name)
        raise UpdateError(f"Couldn't write the new files (the old version was restored): {e}") from e
    for script in (ROOT / "install.sh", *(ROOT / "scripts").glob("*.sh")):
        if script.exists():
            script.chmod(script.stat().st_mode | 0o755)  # zip files don't keep this


def install() -> str | None:
    """Install the newest release if it's newer. Returns its version, or None
    when already up to date."""
    if mode() != "installed":
        raise UpdateError(
            "This copy is managed by Claude's plugin system — update the plugin." if mode() == "plugin"
            else "This is a development copy (a git checkout) — update it with git pull.")
    state.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with desktop.file_lock(state.DATA_DIR / "update.lock"):
        try:
            release = latest_release()
            if not release or not is_newer(release["version"], current_version()):
                return None
            response = requests.get(release["zip"], headers=_HEADERS, timeout=180)
            response.raise_for_status()
        except (requests.RequestException, ValueError, KeyError) as e:
            raise UpdateError(f"Couldn't download the update: {e}") from e
        with tempfile.TemporaryDirectory() as tmp:
            new = _extract(response.content, Path(tmp))
            _verify(new, release["version"])
            _install_requirements(new)
            _smoke_test(new)
            _apply(new, state.DATA_DIR / "backup")
        _save("update.json", {})
        return release["version"]


def install_if_automatic() -> str | None:
    """For launch and the daily run: update quietly when the user left automatic
    updates on. Problems are printed, never fatal."""
    if mode() != "installed" or not auto_enabled():
        return None
    try:
        return install()
    except (UpdateError, OSError) as e:
        print(f"  x update: {e}")
        return None
