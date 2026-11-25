"""Client for ISB's Moodle webservice API (wstoken-based REST).

Owns its own token (in `.env`). The token is only ever obtained in the user's
own browser: the Chrome extension captures it when they press "Log in to
Moodle" in the app, and `save_token` verifies and stores it. `clear_token` is
log out.

MOODLE_URL is hardcoded, not configurable — this only ever talks to ISB's
Moodle (elearn.isb.edu); see plugin.json's description for why that scope
limit is deliberate rather than an oversight.
"""
from __future__ import annotations

import functools
import os
import re
from dataclasses import dataclass
from pathlib import Path

import requests
from dotenv import load_dotenv

MOODLE_URL = "https://elearn.isb.edu"
PROJECT_ROOT = Path(__file__).parent.parent

# Where per-user data lives. Installed as a Claude plugin, that's
# CLAUDE_PLUGIN_DATA (survives plugin updates); run from the repo, it's the
# repo itself. The same source works in both places.
_PLUGIN_DATA = os.environ.get("CLAUDE_PLUGIN_DATA")
ENV_PATH = (Path(_PLUGIN_DATA) if _PLUGIN_DATA else PROJECT_ROOT) / ".env"
STATE_DIR = Path(_PLUGIN_DATA) if _PLUGIN_DATA else PROJECT_ROOT / "data"
SESSION_DIR = STATE_DIR / "browser-profile"  # headless browser profile

load_dotenv(ENV_PATH)

WSTOKEN = os.environ.get("MOODLE_WSTOKEN", "")

# Enrolments that are groupings, not courses ("Section A", "Section B"…).
NOT_COURSES = re.compile(r"^(Section [A-Z]|Summer Internship)$")


class InvalidTokenError(RuntimeError):
    pass


class NoTokenError(RuntimeError):
    def __init__(self):
        super().__init__(
            "Not logged in to Moodle. Open LMS Sync (`cli.py ui`) and press “Log in to Moodle” — "
            "it signs in through your own Chrome with the LMS Sync extension."
        )


@dataclass
class Course:
    id: int
    shortname: str
    fullname: str
    startdate: int
    enddate: int


def reload_token() -> None:
    """Re-reads .env — a login or log out rewrites it while a long-running
    process still has the old token loaded."""
    global WSTOKEN
    os.environ.pop("MOODLE_WSTOKEN", None)  # a removed line must not linger from an earlier load
    load_dotenv(ENV_PATH, override=True)
    WSTOKEN = os.environ.get("MOODLE_WSTOKEN", "")


def _flatten(params: dict) -> dict:
    """Moodle's REST format expects PHP-array-style keys for list params
    (courseids[0]=1&courseids[1]=2), not a repeated bare key — which is what
    `requests` would otherwise produce from a plain list value."""
    flat = {}
    for key, value in params.items():
        if isinstance(value, (list, tuple)):
            for i, v in enumerate(value):
                flat[f"{key}[{i}]"] = v
        else:
            flat[key] = value
    return flat


def _call(wsfunction: str, **params) -> dict | list:
    if not WSTOKEN:
        raise NoTokenError()
    resp = requests.post(
        f"{MOODLE_URL}/webservice/rest/server.php",
        data={
            "wstoken": WSTOKEN,
            "wsfunction": wsfunction,
            "moodlewsrestformat": "json",
            **_flatten(params),
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("exception"):
        if data.get("errorcode") == "invalidtoken":
            raise InvalidTokenError(data.get("message"))
        raise RuntimeError(f"Moodle API error ({wsfunction}): {data.get('message')}")
    return data


def _write_token(wstoken: str | None) -> None:
    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = [l for l in (ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []) if not l.startswith("MOODLE_WSTOKEN=")]
    if wstoken:
        lines.append(f"MOODLE_WSTOKEN={wstoken}")
    ENV_PATH.write_text("".join(f"{l}\n" for l in lines), encoding="utf-8")
    os.chmod(ENV_PATH, 0o600)
    reload_token()
    get_userid.cache_clear()


def save_token(wstoken: str) -> dict:
    """Verify a token against Moodle, and only then store it in .env — a failed
    log in leaves the current one in place."""
    resp = requests.post(
        f"{MOODLE_URL}/webservice/rest/server.php",
        data={"wstoken": wstoken, "wsfunction": "core_webservice_get_site_info", "moodlewsrestformat": "json"},
        timeout=30,
    )
    info = resp.json()
    if not isinstance(info, dict) or "userid" not in info:
        raise ValueError("Moodle didn't accept that token.")
    _write_token(wstoken)
    return info


def clear_token() -> None:
    """Log out on this computer. (The token isn't revoked on Moodle's side —
    the web service offers no way to do that.)"""
    _write_token(None)


def site_info() -> dict:
    return _call("core_webservice_get_site_info")


@functools.lru_cache(maxsize=1)
def get_userid() -> int:
    return site_info()["userid"]


def list_courses() -> list[Course]:
    raw = _call("core_enrol_get_users_courses", userid=get_userid())
    courses = [
        Course(
            id=c["id"],
            shortname=c["shortname"],
            fullname=c["fullname"],
            startdate=c.get("startdate", 0),
            enddate=c.get("enddate", 0),
        )
        for c in raw
    ]
    return [c for c in courses if not NOT_COURSES.match(c.fullname.strip())]


def get_course_contents(course_id: int) -> list[dict]:
    return _call("core_course_get_contents", courseid=course_id)


def get_assignments(course_id: int) -> list[dict]:
    """Raw assignment dicts. `introattachments` (files attached to the
    assignment's own instructions) only exist here — core_course_get_contents
    never exposes them."""
    raw = _call("mod_assign_get_assignments", courseids=[course_id])
    return [a for course in raw.get("courses", []) for a in course.get("assignments", [])]


def get_assignment_list(course_id: int) -> tuple[list[dict], int]:
    """Assignments plus how many assignment modules Moodle refuses to show us
    ("No access rights" — hidden or restricted; faculty do this after a deadline)."""
    raw = _call("mod_assign_get_assignments", courseids=[course_id])
    hidden = sum(1 for w in raw.get("warnings", []) if w.get("item") == "module")
    return [a for course in raw.get("courses", []) for a in course.get("assignments", [])], hidden


def get_submission_status(assign_id: int) -> dict:
    return _call("mod_assign_get_submission_status", assignid=assign_id, userid=get_userid())


def get_quizzes(course_id: int) -> list[dict]:
    return _call("mod_quiz_get_quizzes_by_courses", courseids=[course_id]).get("quizzes", [])


def get_action_events(course_id: int, since: int) -> list[dict]:
    """Upcoming "you have something to do" calendar events of any activity type."""
    return _call("core_calendar_get_action_events_by_course", courseid=course_id, timesortfrom=since).get("events", [])


def get_forum_discussions(forum_id: int) -> list[dict]:
    return _call("mod_forum_get_forum_discussions", forumid=forum_id).get("discussions", [])


def get_submission_files(assign_id: int) -> dict:
    """{'files': [{filename, fileurl, filesize, timemodified}], 'onlinetext': str | None}
    for the current submission (group-attributed if it's a team submission —
    Moodle returns the group's submission for any member's own userid)."""
    status = _call("mod_assign_get_submission_status", assignid=assign_id, userid=get_userid())
    attempt = status.get("lastattempt", {})
    submission = attempt.get("teamsubmission") or attempt.get("submission")
    if not submission or submission.get("status") != "submitted":
        return {"files": [], "onlinetext": None}

    files = []
    onlinetext = None
    for plugin in submission.get("plugins", []):
        if plugin.get("type") == "file":
            for area in plugin.get("fileareas", []):
                for f in area.get("files", []):
                    files.append(
                        {
                            "filename": f["filename"],
                            "fileurl": f["fileurl"],
                            "filesize": f.get("filesize", 0),
                            "timemodified": f.get("timemodified", 0),
                        }
                    )
        elif plugin.get("type") == "onlinetext":
            for field in plugin.get("editorfields", []):
                if field.get("text"):
                    onlinetext = field["text"]
    return {"files": files, "onlinetext": onlinetext}


def download_via_wstoken(fileurl: str) -> bytes:
    sep = "&" if "?" in fileurl else "?"
    resp = requests.get(f"{fileurl}{sep}token={WSTOKEN}", timeout=60)
    resp.raise_for_status()
    return resp.content
