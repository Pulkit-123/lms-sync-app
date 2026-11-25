"""Prepares a course folder: creates it, plus the one `Claude/` folder inside
it where course chats/projects keep their working files and anything Claude
needs that isn't course content.

Everything here is create-if-missing, never overwrite — `Claude/` is Claude's
(and the user's) to edit freely after this first touch, and the course
folder itself is the user's to organize by hand.
"""
from __future__ import annotations

import sys
from pathlib import Path

import moodle_client

CLAUDE_DIR_NAME = "Claude"

ROOT_STUB = f"@{CLAUDE_DIR_NAME}/CLAUDE.md\n"

CLAUDE_MD = """\
# {name}

Coursework for {name} at ISB (Moodle course id {course_id}).

## Where things go
- The course folder (the one containing this `Claude/` folder) is the
  user's: files from Moodle are downloaded into it flat, and the user
  organizes them by hand. Don't move, rename or delete anything outside
  `Claude/` unless asked.
- Everything of yours belongs in this `Claude/` folder: working files,
  notes, analysis, drafts, scripts, scratch data, anything you need that
  isn't course content. A subfolder per task is fine.
- `CONTEXT.md` (what's been decided/learned about this course) and
  `JOURNAL.md` (the reasoning and log) live here too — keep them current.

## Honor code / what help is allowed
## Getting files from Moodle
Downloads land in `Moodle/<section name>/` (matching the sections on Moodle) unless
the course's "Sort into section folders" switch is off; the user may have moved
them since. Files are fetched by the `lms-sync` tool at `{root}`. Run it directly
rather than reaching Moodle any other way:

```bash
{cli} sync {course_id}   # download what's new/changed for what's ticked
{cli} ui                  # browser UI: change what's ticked, pick files
```

What's ticked (course / sections / single files) is chosen in the UI. A
file that changes on Moodle after it was downloaded is fetched again as
"<name> (updated <date>)" next to the original — the original is never
touched. If a command says the token or login has expired, tell the user to
log in again from the LMS Sync app (`cli.py ui`); don't try to
work around it.

Discussion forums and announcements aren't synced, but you can read them
live through the Moodle API (`moodle_client` in that folder) when asked.
"""

CONTEXT_MD = """\
# What we know — {name}

Claude Code reads this at the start of every session, so anything here carries over
instead of being explained again.

## Scope
{name} — Moodle course id {course_id}. Fill in: instructors, session/topic
schedule, assignment weights, anything about how the course really runs.

## Decisions
_None yet._

## Gotchas
_None yet._
"""

JOURNAL_MD = """\
# How we got here — {name}

`CONTEXT.md` holds what was decided. This holds the reasoning, and anything
still being argued — the part that otherwise disappears when a chat window closes.

## Open threads
_None._

## Log
"""


def prepare(folder: Path, name: str, course_id: int) -> None:
    claude = folder / CLAUDE_DIR_NAME
    claude.mkdir(parents=True, exist_ok=True)
    cli = f'"{sys.executable}" "{Path(__file__).parent / "cli.py"}"'
    fields = {"name": name or f"Course {course_id}", "course_id": course_id, "root": moodle_client.PROJECT_ROOT, "cli": cli}
    for filename, template in (("CLAUDE.md", CLAUDE_MD), ("CONTEXT.md", CONTEXT_MD), ("JOURNAL.md", JOURNAL_MD)):
        target = claude / filename
        if not target.exists():
            target.write_text(template.format(**fields), encoding="utf-8")
    root_claude = folder / "CLAUDE.md"
    if not root_claude.exists():
        root_claude.write_text(ROOT_STUB, encoding="utf-8")
