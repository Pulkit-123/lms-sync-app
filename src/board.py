"""A course's "board": its announcements and what is due when.

Deadlines come from four places, because Moodle alone isn't reliable:
  assignment / quiz / event  read from Moodle. Remembered once seen — faculty
        often hide an assignment after (or before) its date, and Moodle then
        refuses to show it ("No access rights"), so an entry that disappears is
        kept and marked `gone`, never dropped.
  manual   added by the user (with optional notes and links), either by hand
        or by confirming a date the app spotted in an announcement or an
        activity's description (many deadlines only exist as prose).
Assignment entries also carry their instructions, the files attached to them
and the other files in the same course section (instruction PDFs usually sit
right next to the submission link).

State lives under the course's "board" key (see state.py): which announcements
the user has read, which the app has already reported, and the deadlines.
Pure logic (text conversion, date spotting, merging) is kept separate from the
Moodle calls at the bottom.
"""
from __future__ import annotations

import datetime
import html
import re
import time
import uuid
from html.parser import HTMLParser

import catalog
import download
import moodle_client
import state

DUE_SOON_S = 48 * 3600
MOODLE_SOURCES = ("assignment", "quiz", "event")


# ---- announcement text -------------------------------------------------------

class _Text(HTMLParser):
    """Moodle's announcement HTML -> plain text + links. We never inject their
    HTML into our page."""

    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "ul", "ol", "table"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[dict] = []
        self._skip = 0
        self._href: str | None = None
        self._label: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        if tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("• ")
        if tag == "a":
            href = dict(attrs).get("href") or ""
            self._href, self._label = (href if href.lower().startswith(("http://", "https://")) else None), []

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        if tag in ("p", "div", "tr", "table"):
            self.parts.append("\n")
        if tag == "a" and self._href:
            label = "".join(self._label).strip() or self._href
            self.links.append({"label": label, "url": self._href})
            self._href = None

    def handle_data(self, data):
        if self._skip:
            return
        self.parts.append(data)
        if self._href:
            self._label.append(data)


def to_text(markup: str) -> tuple[str, list[dict]]:
    parser = _Text()
    parser.feed(markup or "")
    parser.close()
    text = "".join(parser.parts).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text.replace("\r", ""))
    return re.sub(r"\n(?:\s*\n)+", "\n\n", text).strip(), parser.links


# ---- spotting dates in prose -------------------------------------------------

_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
_MONTH = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_DATE = re.compile(
    rf"\b(?:(\d{{1,2}})(?:st|nd|rd|th)?\s+{_MONTH}|{_MONTH}\s+(\d{{1,2}})(?:st|nd|rd|th)?)\b\.?,?(?:\s*(\d{{4}}))?",
    re.IGNORECASE,
)
_TIME = re.compile(r"\b(?:(\d{1,2})(?:[:.](\d{2}))?\s*([ap])\.?\s?m\b\.?|([01]?\d|2[0-3]):([0-5]\d)\b)", re.IGNORECASE)
_DUE_WORDS = re.compile(
    r"\b(due|deadline|submit|submitted|submission|before|latest by|no later than"
    r"|by (?:mon|tues|wednes|thurs|fri|satur|sun)day|by \d{1,2}(?::\d\d)?\s*[ap]\.?m)\b", re.IGNORECASE)


def _clock(m: re.Match) -> tuple[int, int] | None:
    if m.group(3):
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        if not 1 <= hour <= 23 or minute > 59:
            return None
        if hour > 12:  # "23:59 PM", as people write it: already a 24-hour time
            return hour, minute
        return hour % 12 + (12 if m.group(3).lower() == "p" else 0), minute
    return int(m.group(4)), int(m.group(5))


def spot_dates(text: str, posted: int) -> list[dict]:
    """Dates that read like deadlines: [{due, time_assumed, snippet}]. Only a
    suggestion for the user to confirm — never tracked on its own."""
    found: dict[int, dict] = {}
    for sentence in re.split(r"\n+|(?<=[.!?])\s+(?=[A-Z•])", text):
        if not _DUE_WORDS.search(sentence):
            continue
        for m in _DATE.finditer(sentence):
            if m.group(3) and not m.group(3)[0].isupper():  # "you may 2…" is not the 2nd of May
                continue
            day = int(m.group(1) or m.group(4))
            word = (m.group(2) or m.group(3)).lower()[:3]
            month = next(i for i, name in enumerate(_MONTHS, 1) if name.startswith(word))
            base = datetime.datetime.fromtimestamp(posted)
            year = int(m.group(5)) if m.group(5) else base.year
            times = [(abs(t.start() - m.start()), c) for t in _TIME.finditer(sentence) if (c := _clock(t))]
            hour, minute = min(times)[1] if times else (23, 59)
            try:
                when = datetime.datetime(year, month, day, hour, minute)
                if not m.group(5) and (base - when).days > 60:  # "5 January" posted in December
                    when = when.replace(year=year + 1)
            except ValueError:
                continue
            due = int(when.timestamp())
            found.setdefault(due, {"due": due, "time_assumed": not times, "snippet": sentence.strip()[:220]})
    return sorted(found.values(), key=lambda d: d["due"])


# ---- merging and status ------------------------------------------------------

def merge_deadlines(stored: dict, fetched: dict, sources_ok: set[str]) -> dict:
    """Update the remembered deadlines with what Moodle shows now. The user's
    "done" tick survives; a moved date is noted; a Moodle entry that vanished is
    kept and marked gone (only if its source was actually read this time)."""
    merged = {key: dict(entry) for key, entry in stored.items()}
    for key, new in fetched.items():
        old = merged.get(key, {})
        entry = {**old, **new, "gone": False}
        if old.get("due") and new.get("due") and old["due"] != new["due"]:
            entry["previous_due"] = old["due"]
        merged[key] = entry
    for key, entry in merged.items():
        if key not in fetched and entry.get("source") in sources_ok:
            entry["gone"] = True
    return merged


def status_of(entry: dict, now: float) -> str:
    if entry.get("submitted"):
        return "submitted"
    if entry.get("done"):
        return "done"
    due = entry.get("due")
    if not due:
        return "no-date"
    if due < now:
        return "past" if entry.get("source") in ("manual", "event") or entry.get("gone") else "overdue"
    return "soon" if due - now <= DUE_SOON_S else "upcoming"


def counts(board: dict, now: float | None = None) -> dict:
    """Cheap numbers for the sidebar, from what's already remembered."""
    now = now or time.time()
    unread = len(set(board.get("known", [])) - set(board.get("read", [])))
    soon = sum(1 for e in board.get("deadlines", {}).values() if status_of(e, now) == "soon")
    return {"unread": unread, "due_soon": soon}


# ---- reading Moodle ----------------------------------------------------------

def _files_around(sections: list[catalog.Section], module_sections: dict[int, str], cmid: int) -> dict:
    """An assignment's own attachments, and the other files in its section (and
    that section's subsections)."""
    home = module_sections.get(cmid, "")
    own, related = [], []
    for section in sections:
        for item in section.items:
            if item.module_id == cmid:
                own.append({"key": item.key, "name": item.name})
            elif home and home in (section.id, section.parent) and item.kind != "link":
                related.append({"key": item.key, "name": item.name})
    return {"files": own, "related": related}


def _assignment_entries(course_id: int, contents: list[dict]) -> tuple[dict, int]:
    assignments, hidden = moodle_client.get_assignment_list(course_id)
    sections = catalog.sections_from(contents, {a["cmid"]: a for a in assignments})
    module_sections = {m["id"]: str(s["id"]) for s in contents for m in s.get("modules", [])}
    entries = {}
    for a in assignments:
        if a.get("nosubmissions"):
            continue
        try:
            attempt = moodle_client.get_submission_status(a["id"]).get("lastattempt") or {}
        except Exception:  # noqa: BLE001 — a status we can't read mustn't hide the deadline
            attempt = {}
        submission = attempt.get("teamsubmission") or attempt.get("submission") or {}
        due = attempt.get("extensionduedate") or a.get("duedate") or a.get("cutoffdate") or 0
        instructions, links = to_text(f"{a.get('intro') or ''}\n{a.get('activity') or ''}")
        entries[f"assign:{a['cmid']}"] = {
            "source": "assignment",
            "title": download.clean_text(a["name"]),
            "due": due,
            "team": bool(a.get("teamsubmission")),
            "submitted": submission.get("status") == "submitted",
            "url": f"{moodle_client.MOODLE_URL}/mod/assign/view.php?id={a['cmid']}",
            "instructions": instructions[:6000],
            "links": links[:15],
            **_files_around(sections, module_sections, a["cmid"]),
        }
    return entries, hidden


def _quiz_entries(course_id: int) -> dict:
    return {
        f"quiz:{q['coursemodule']}": {
            "source": "quiz",
            "title": download.clean_text(q["name"]),
            "due": q.get("timeclose") or 0,
            "url": f"{moodle_client.MOODLE_URL}/mod/quiz/view.php?id={q['coursemodule']}",
        }
        for q in moodle_client.get_quizzes(course_id)
    }


def _event_entries(course_id: int) -> dict:
    """Other activities with a date (group choice, feedback…); assignments and
    quizzes are already covered above."""
    return {
        f"event:{e['id']}": {
            "source": "event",
            "title": html.unescape(download.clean_text(e["name"])),
            "due": e.get("timesort") or 0,
            "url": e.get("url", ""),
        }
        for e in moodle_client.get_action_events(course_id, int(time.time()) - 14 * 86400)
        if e.get("modulename") not in ("assign", "quiz")
    }


def _described_activities(contents: list[dict]) -> list[dict]:
    """Activities whose description might carry a deadline (e.g. a group-choice
    "form your groups by …")."""
    found = []
    for section in contents:
        for mod in section.get("modules", []):
            if mod.get("modname") in ("assign", "forum") or mod.get("uservisible") is False:
                continue
            text, _ = to_text(mod.get("description", ""))
            if text:
                found.append({"id": mod["id"], "title": download.clean_text(mod.get("name", "")), "text": text,
                              "url": mod.get("url", "")})
    return found


def _announcements(contents: list[dict]) -> list[dict]:
    forums = [
        mod["instance"]
        for section in contents
        for mod in section.get("modules", [])
        if mod.get("modname") == "forum" and "announcement" in mod.get("name", "").lower()
    ]
    items = []
    for forum_id in forums:
        for d in moodle_client.get_forum_discussions(forum_id):
            text, links = to_text(d.get("message", ""))
            items.append({
                "id": d["discussion"],
                "subject": html.unescape(d.get("name") or d.get("subject") or "(no subject)"),
                "author": html.unescape(d.get("userfullname", "")),
                "posted": d.get("created", 0),
                "text": text,
                "links": links,
                "attachments": [a.get("filename", "") for a in d.get("attachments") or []],
                "url": f"{moodle_client.MOODLE_URL}/mod/forum/discuss.php?d={d['discussion']}",
            })
    return sorted(items, key=lambda a: a["posted"], reverse=True)


# ---- the operations the app uses ---------------------------------------------

def refresh(course_id: int) -> dict:
    """Read Moodle, remember what's there, and return the board for the UI.
    One unavailable source is reported in `problems`, not fatal."""
    fetched: dict = {}
    sources_ok: set[str] = set()
    problems: list[str] = []
    announcements: list[dict] = []
    hidden = 0

    def attempt(label: str, work):
        try:
            return work()
        except (moodle_client.NoTokenError, moodle_client.InvalidTokenError):
            raise
        except Exception as e:  # noqa: BLE001
            problems.append(f"Couldn't read {label}: {e}")
            return None

    contents = attempt("the course page", lambda: moodle_client.get_course_contents(course_id))
    result = attempt("assignments", lambda: _assignment_entries(course_id, contents or []))
    if result:
        fetched.update(result[0])
        hidden = result[1]
        sources_ok.add("assignment")
    if contents is None and result:  # without the course page we can't know an assignment's files; keep the old ones
        for entry in result[0].values():
            entry.pop("files", None)
            entry.pop("related", None)
    for source, label, work in (("quiz", "quizzes", _quiz_entries), ("event", "calendar events", _event_entries)):
        entries = attempt(label, lambda work=work: work(course_id))
        if entries is not None:
            fetched.update(entries)
            sources_ok.add(source)
    announcements = attempt("announcements", lambda: _announcements(contents)) if contents is not None else None
    announcements = announcements or []

    fresh: list[dict] = []

    def change(course: dict) -> None:
        board = course["board"]
        first_look = not board["checked_at"]
        known = set(board["known"])
        fresh.extend([] if first_look else [a for a in announcements if a["id"] not in known])
        board["known"] = sorted(known | {a["id"] for a in announcements})
        board["deadlines"] = merge_deadlines(board["deadlines"], fetched, sources_ok)
        board["checked_at"] = datetime.datetime.now().isoformat(timespec="seconds")

    course = state.update_course(course_id, change)
    view = view_of(course["board"], announcements, activities=_described_activities(contents or []),
                   downloaded=course["downloaded"])
    view.update(hidden_assignments=hidden, problems=problems, new_announcements=[a["subject"] for a in fresh])
    return view


def view_of(board: dict, announcements: list[dict], now: float | None = None,
            activities: list[dict] | None = None, downloaded: dict | None = None) -> dict:
    now = now or time.time()
    read = set(board["read"])
    saved = downloaded or {}
    deadlines = []
    for k, e in board["deadlines"].items():
        entry = {"key": k, **e, "status": status_of(e, now)}
        for kind in ("files", "related"):
            if e.get(kind):
                entry[kind] = [{**f, "saved": f["key"] in saved} for f in e[kind]]
        deadlines.append(entry)
    # What still needs doing first (soonest first), then everything settled (latest first).
    open_states = ("overdue", "soon", "upcoming", "no-date")
    deadlines.sort(key=lambda d: (0, d["due"] or 2**40) if d["status"] in open_states else (1, -(d["due"] or 0)))
    tracked = {e.get("due") for e in board["deadlines"].values()}
    suggestions = []
    sources = [(f"{a['id']}", a["subject"], a["text"], a["posted"], a.get("url", ""), "announcement") for a in announcements]
    sources += [(f"mod{m['id']}", m["title"], m["text"], int(now), m["url"], "activity description") for m in activities or []]
    for ref, title, text, posted, url, where in sources:
        for s in spot_dates(text, posted):
            key = f"{ref}:{s['due']}"
            if s["due"] > now - 86400 and s["due"] not in tracked and key not in board["dismissed"]:
                suggestions.append({**s, "key": key, "title": title, "url": url, "where": where})
                tracked.add(s["due"])
    return {
        "deadlines": deadlines,
        "suggestions": sorted(suggestions, key=lambda s: s["due"]),
        "announcements": [{**a, "unread": a["id"] not in read} for a in announcements],
        "checked_at": board["checked_at"],
    }


def mark_read(course_id: int, announcement_id: int | None) -> None:
    """None = everything seen so far."""
    def change(course: dict) -> None:
        board = course["board"]
        ids = board["known"] if announcement_id is None else [announcement_id]
        board["read"] = sorted(set(board["read"]) | set(ids))

    state.update_course(course_id, change)


def set_done(course_id: int, key: str, done: bool) -> None:
    def change(course: dict) -> None:
        if key not in course["board"]["deadlines"]:
            raise LookupError("That deadline isn't tracked.")
        course["board"]["deadlines"][key]["done"] = bool(done)

    state.update_course(course_id, change)


def clean_links(raw) -> list[str]:
    """The user's links, one per line: web addresses only ("docs.google.com/x"
    gets https://)."""
    lines = raw.split() if isinstance(raw, str) else [str(x).strip() for x in raw or []]
    links = []
    for link in lines:
        if not link:
            continue
        if "://" not in link and re.match(r"^[\w-]+(\.[\w-]+)+(/|$|\?)", link):
            link = "https://" + link
        if not re.match(r"^https?://[^\s/]+", link, re.IGNORECASE):
            raise ValueError(f"Not a web link: {link[:80]}")
        links.append(link[:2000])
    return links[:10]


def _manual_entry(title: str, due: int, notes: str, links) -> dict:
    title = download.clean_text(title)[:200]
    if not title or due <= 0:
        raise ValueError("A deadline needs a title and a date.")
    return {"source": "manual", "title": title, "due": due, "notes": str(notes or "").strip()[:4000], "links": clean_links(links)}


def add_manual(course_id: int, title: str, due: int, notes: str = "", links=()) -> str:
    entry = _manual_entry(title, due, notes, links)
    key = f"manual:{uuid.uuid4().hex[:12]}"
    state.update_course(course_id, lambda c: c["board"]["deadlines"].__setitem__(key, entry))
    return key


def edit_manual(course_id: int, key: str, title: str, due: int, notes: str = "", links=()) -> None:
    entry = _manual_entry(title, due, notes, links)

    def change(course: dict) -> None:
        old = course["board"]["deadlines"].get(key)
        if old is None or old["source"] != "manual":
            raise ValueError("Only deadlines you added can be edited.")
        course["board"]["deadlines"][key] = {**old, **entry}

    state.update_course(course_id, change)


def remove(course_id: int, key: str) -> None:
    """Only entries Moodle can't bring back on its own: the user's, or vanished ones."""
    def change(course: dict) -> None:
        entry = course["board"]["deadlines"].get(key)
        if entry is None or not (entry["source"] == "manual" or entry.get("gone")):
            raise ValueError("Only your own deadlines, or ones Moodle no longer shows, can be removed.")
        del course["board"]["deadlines"][key]

    state.update_course(course_id, change)


def dismiss_suggestion(course_id: int, key: str) -> None:
    def change(course: dict) -> None:
        course["board"]["dismissed"] = sorted(set(course["board"]["dismissed"]) | {key})

    state.update_course(course_id, change)


def check_tracked() -> dict:
    """For the daily run: refresh every course in the syncing list. Returns what's
    worth telling the user: new announcements and open deadlines within 48h."""
    news: list[str] = []
    due: list[str] = []
    problems: list[str] = []
    for course_id, cfg in sorted(state.all_courses().items()):
        if not state.is_syncing(cfg):
            continue
        name = cfg["name"] or str(course_id)
        try:
            view = refresh(course_id)
        except Exception as e:  # noqa: BLE001 — one course must not stop the rest
            problems.append(f"{name}: {e}")
            continue
        news.extend(f"{name}: {subject}" for subject in view["new_announcements"])
        for d in view["deadlines"]:
            if d["status"] == "soon":
                due_at = datetime.datetime.fromtimestamp(d["due"])
                when = f"{due_at:%a} {due_at.day} {due_at:%b, %H:%M}"
                due.append(f"{name}: {d['title']} — {when}")
    return {"announcements": news, "due_soon": due, "problems": problems}


def calendar(now: float | None = None) -> dict:
    """Every remembered deadline across courses, for the side panel (no Moodle calls)."""
    now = now or time.time()
    entries, checked = [], []
    for course_id, cfg in state.all_courses().items():
        board = cfg["board"]
        if board["checked_at"] and state.is_syncing(cfg):
            checked.append(board["checked_at"])
        elif not board["checked_at"] and state.is_syncing(cfg):
            checked.append("")  # a syncing course never looked at
        for key, e in board["deadlines"].items():
            if e.get("due"):
                entries.append({
                    "course_id": course_id, "course": cfg["name"] or str(course_id), "key": key, "title": e["title"],
                    "due": e["due"], "status": status_of(e, now), "source": e["source"],
                    "notes": e.get("notes", ""), "url": e.get("url", ""),
                })
    return {"deadlines": sorted(entries, key=lambda d: d["due"]), "oldest_check": min(checked) if checked else ""}

