"""The one place sync state lives: data/state.json (gitignored; override the
directory with LMS_SYNC_DATA).

Per course:
  folder      where its files are downloaded (flat), chosen in the UI
  selection   what to keep syncing: whole course / sections / single items,
              minus `excluded` (items un-ticked under a ticked section/course)
  downloaded  item key -> what was fetched (size/timemodified at the time)
  flags       "this changed on Moodle after you downloaded it" notices
  locked_waiting  ticked locked (watermarked) files still needing a browser login
  board       announcements read/reported and tracked deadlines (see board.py)
  syncing     in the daily sync? (None = automatic; see is_syncing)
  in_extension  show this course in the Chrome extension's "save to a course" list
  organize    True: new files go in <folder>/Moodle/<Section name>/; False: flat in <folder>

Items are remembered by Moodle's own identifiers, never by local path — so
moving or renaming a downloaded file can't make the tool re-download it or
lose track of it.
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

import desktop
import moodle_client

DATA_DIR = Path(os.environ.get("LMS_SYNC_DATA") or moodle_client.STATE_DIR)
STATE_PATH = DATA_DIR / "state.json"
LOCK_PATH = DATA_DIR / "state.lock"


def _new_course() -> dict:
    return {
        "name": "",
        "folder": None,
        "selection": {"course": False, "sections": [], "items": [], "excluded": []},
        "downloaded": {},
        "flags": {},
        "locked_waiting": 0,
        "organize": True,
        "in_extension": True,
        # Is this course in the daily sync? None = never chosen: it follows having
        # a folder and something ticked. True/False = the user's own switch.
        "syncing": None,
        "board": {"known": [], "read": [], "deadlines": {}, "dismissed": [], "checked_at": None},
    }


@contextmanager
def _locked():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with desktop.file_lock(LOCK_PATH):
        yield


def _replace(src: str, dest: Path) -> None:
    """os.replace, retried: on Windows a virus scanner or indexer can hold the
    target open for a moment."""
    for attempt in range(20):
        try:
            os.replace(src, dest)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.05)


def _read() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"courses": {}}


def _write(data: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        _replace(tmp, STATE_PATH)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _with_defaults(course: dict | None) -> dict:
    merged = _new_course()
    merged.update(course or {})
    merged["selection"] = {**_new_course()["selection"], **merged["selection"]}
    merged["board"] = {**_new_course()["board"], **merged["board"]}
    return merged


DAILY_PATH = DATA_DIR / "daily_status.json"


def record_daily(result: dict) -> None:
    """What the last unattended run did, so the UI can show it ran even when
    it found nothing (a quiet run sends no notification)."""
    with _locked():
        fd, tmp = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(result, f)
        _replace(tmp, DAILY_PATH)


def last_daily() -> dict | None:
    try:
        return json.loads(DAILY_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None


def has_selection(sel: dict) -> bool:
    return bool(sel["course"] or sel["sections"] or sel["items"])


def is_syncing(course: dict) -> bool:
    """Whether the unattended sync (files, announcements, deadlines) covers it.
    A folder is always required."""
    if not course["folder"]:
        return False
    return has_selection(course["selection"]) if course["syncing"] is None else bool(course["syncing"])


def get_course(course_id: int) -> dict:
    with _locked():
        return copy.deepcopy(_with_defaults(_read()["courses"].get(str(course_id))))


def all_courses() -> dict[int, dict]:
    with _locked():
        return {int(cid): _with_defaults(c) for cid, c in _read()["courses"].items()}


def update_course(course_id: int, change: Callable[[dict], None]) -> dict:
    """Read-modify-write one course under the lock, so the UI and the daily
    job never overwrite each other's changes."""
    with _locked():
        data = _read()
        course = _with_defaults(data["courses"].get(str(course_id)))
        change(course)
        data["courses"][str(course_id)] = course
        _write(data)
        return copy.deepcopy(course)
