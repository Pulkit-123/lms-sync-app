"""Turns a course's live Moodle structure into sections of downloadable items.

Every item has a stable `key` (what the state file remembers it by — never a
local path, since the user moves files around freely) and a `name` that is
both what the UI shows and the filename it will get locally.

Item kinds:
  file             a regular resource / folder / book-chapter / page file, or a
                   file attached to an assignment's own instructions
  watermarked      a filewithwatermark module — no API, needs a browser session
  link             a Moodle "URL" resource (article, course pack, website):
                   `fileurl` is where it points. What gets saved depends on
                   links.classify — the file itself, or a PDF snapshot of the page
  submission-file  a file the student (or their group) turned in
  submission-text  an online-text submission, saved as .html
"""
from __future__ import annotations

import json
import os
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import download
import moodle_client

SUBMISSIONS_SECTION_ID = "submissions"


@dataclass
class Item:
    key: str
    name: str
    size: int
    timemodified: int
    kind: str
    module_id: int
    module_name: str
    modname: str
    fileurl: str = ""
    inline: str = ""
    note: str = ""  # the activity's description on Moodle (shown once, on its first item)


@dataclass
class Section:
    id: str
    name: str
    items: list[Item] = field(default_factory=list)
    parent: str = ""  # id of the section this one is nested in (a Moodle "subsection")


def _local_names(module_name: str, filenames: list[str]) -> list[str]:
    """Local filenames for one module's files, in Moodle's order. A Moodle
    "Book" exports every chapter as its own file all literally named
    index.html — genuinely different content, not duplicates — so those take
    the module's name plus a number; any other repeated name just gets a
    number."""
    totals = Counter(filenames)
    seen: Counter = Counter()
    out = []
    for fn in filenames:
        seen[fn] += 1
        n, multi = seen[fn], totals[fn] > 1
        if fn.lower() in ("index.html", "index.htm"):
            base = module_name or "page"
            out.append(f"{base} ({n}).html" if multi else f"{base}.html")
        elif multi:
            stem, ext = os.path.splitext(fn)
            out.append(f"{stem} ({n}){ext}")
        else:
            out.append(fn)
    return [download.safe_filename(x) for x in out]


def _section_name(sec: dict) -> str:
    name = download.clean_text(sec.get("name", ""))
    return name or ("General" if sec.get("section") == 0 else f"Section {sec.get('section')}")


def _module_items(mod: dict, assigns: dict[int, dict]) -> list[Item]:
    mid, modname = mod["id"], mod.get("modname", "")
    mname = download.clean_text(mod.get("name", ""))

    if modname == "filewithwatermark":
        return [Item(f"wm:{mid}", mname or f"Locked file {mid}", 0, 0, "watermarked", mid, mname, modname)]

    if modname == "url":
        target = next((c.get("fileurl") for c in mod.get("contents") or [] if c.get("type") == "url" and c.get("fileurl")), "")
        if not target:
            return []
        return [Item(f"link:{mid}", mname or target, 0, 0, "link", mid, mname, modname, fileurl=target)]

    if modname == "assign":
        # Everything attached to the instructions: attachments, files embedded in
        # the description, and activity-instruction attachments. Only
        # mod_assign_get_assignments exposes these.
        assignment = assigns.get(mid, {})
        files = list({
            a["fileurl"]: {"filename": a["filename"], "fileurl": a["fileurl"], "filesize": a.get("filesize", 0),
                           "timemodified": a.get("timemodified", 0)}
            for field_name in ("introattachments", "introfiles", "activityattachments")
            for a in (assignment.get(field_name) or [])
        }.values())
    else:
        files = [
            c for c in (mod.get("contents") or []) if c.get("type") == "file" and c.get("fileurl")
        ]

    names = _local_names(mname, [f["filename"] for f in files])
    return [
        Item(
            key=f"mod:{mid}:{name}",
            name=name,
            size=f.get("filesize", 0),
            timemodified=f.get("timemodified", 0),
            kind="file",
            module_id=mid,
            module_name=mname,
            modname=modname,
            fileurl=f["fileurl"],
        )
        for f, name in zip(files, names)
    ]


def _submission_of(assignment: dict) -> tuple[dict, dict | None]:
    try:
        return assignment, moodle_client.get_submission_files(assignment["id"])
    except Exception:  # noqa: BLE001 — one unreadable assignment shouldn't hide the rest
        return assignment, None


def _submission_items(assigns: dict[int, dict]) -> list[Item]:
    items = []
    moodle_client.get_userid()  # cached once up front, not raced by the workers below
    # One Moodle round-trip per assignment — sequentially that's what made
    # assignment-heavy courses take ~9s to open.
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_submission_of, assigns.values()))
    for a, sub in results:
        if sub is None:
            continue
        aname = download.clean_text(a["name"])
        for f in sub["files"]:
            items.append(
                Item(
                    key=f"sub:{a['id']}:{f['filename']}",
                    name=download.safe_filename(f"Submitted - {f['filename']}"),
                    size=f["filesize"],
                    timemodified=f["timemodified"],
                    kind="submission-file",
                    module_id=a["cmid"],
                    module_name=aname,
                    modname="assign",
                    fileurl=f["fileurl"],
                )
            )
        if sub["onlinetext"]:
            items.append(
                Item(
                    key=f"sub:{a['id']}:onlinetext",
                    name=download.safe_filename(f"Submitted - {aname}.html"),
                    size=len(sub["onlinetext"]),
                    timemodified=0,
                    kind="submission-text",
                    module_id=a["cmid"],
                    module_name=aname,
                    modname="assign",
                    inline=sub["onlinetext"],
                )
            )
    return items


def _note_of(mod: dict) -> str:
    return " ".join(download.clean_text(mod.get("description", "")).split())[:600]


def sections_from(contents: list[dict], assigns: dict[int, dict]) -> list[Section]:
    """Moodle's course structure as downloadable sections (pure). A nested
    "subsection" comes from Moodle as just another section at the end of the
    course; here it is put back under its parent and linked to it, so ticking
    the parent covers it. A parent with nothing of its own is kept for that."""
    parent_of: dict[str, str] = {}
    for sec in contents:
        for mod in sec.get("modules", []):
            if mod.get("modname") == "subsection":
                try:
                    parent_of[str(json.loads(mod.get("customdata") or "{}")["sectionid"])] = str(sec["id"])
                except (ValueError, KeyError, TypeError):
                    pass

    built: dict[str, Section] = {}
    for sec in contents:
        items = []
        for mod in sec.get("modules", []):
            if mod.get("uservisible") is False:
                continue
            found = _module_items(mod, assigns)
            if found:
                found[0].note = _note_of(mod)
            items.extend(found)
        sid = str(sec["id"])
        built[sid] = Section(id=sid, name=_section_name(sec), items=items, parent=parent_of.get(sid, ""))

    children: dict[str, list[Section]] = {}
    for section in built.values():
        if section.parent in built and section.items:
            children.setdefault(section.parent, []).append(section)

    ordered = []
    for section in built.values():
        if section.parent in built:
            continue  # placed right after its parent below
        kids = children.get(section.id, [])
        if section.items or kids:
            ordered.append(section)
            ordered.extend(kids)
    return ordered


def fetch(course_id: int) -> list[Section]:
    contents = moodle_client.get_course_contents(course_id)
    assigns = {a["cmid"]: a for a in moodle_client.get_assignments(course_id)}
    sections = sections_from(contents, assigns)

    submissions = _submission_items(assigns)
    if submissions:
        sections.append(Section(id=SUBMISSIONS_SECTION_ID, name="My submissions", items=submissions))
    return sections
