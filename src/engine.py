"""Downloads what the user ticked, flat, into their chosen course folder.

Deliberately dumb about layout: files keep their Moodle name, and the only
placement rule is mirroring Moodle's own sections —
<course folder>/Moodle/<Section name>/ (or flat in the course folder when
the course's "organize" setting is off). The user moves files wherever they
like afterwards. What keeps that from causing re-downloads is state.py
remembering items by Moodle's own ids — not where they ended up — so the
layout only ever decides where a *new* file lands.

Never deletes or moves anything. If Moodle replaces a file that was already
downloaded, the new version is fetched alongside as "<name> (updated <date>)"
and flagged; the original (which the user may have moved, renamed or
annotated) is left exactly as it is.
"""
from __future__ import annotations

import datetime
import os
import shutil
import threading
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field
from pathlib import Path

import catalog
import browser
import course_folder
import desktop
import download
import links
import moodle_client
import state

MOODLE_DIR = "Moodle"

_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()


def _course_lock(course_id: int) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(course_id, threading.Lock())


@dataclass
class Summary:
    course_id: int
    label: str
    new: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    needs_login: int = 0
    errors: list[str] = field(default_factory=list)
    skipped: str = ""
    auth_error: bool = False
    # Ticked items that need a sign-in the unattended run can't do. The UI hands
    # these to the Chrome extension, which fetches them in the user's own Chrome.
    pending_login: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def is_selected(item: catalog.Item, section_id: str, sel: dict, parent_id: str = "") -> bool:
    """A ticked section covers its nested subsections too."""
    if item.key in sel["excluded"]:
        return False
    return bool(
        sel["course"] or section_id in sel["sections"] or (parent_id and parent_id in sel["sections"])
        or item.key in sel["items"]
    )


def _unique_path(dest: Path, name: str, module_name: str) -> Path:
    """Never overwrite whatever's already there — it might be a different
    file the user put in the folder. Same name -> try prefixing the Moodle
    module's name (usually what tells two "Slides.pdf" apart), then number."""
    path = dest / name
    if not path.exists():
        return path
    if module_name:
        prefixed = dest / download.safe_filename(f"{module_name} - {name}")
        if not prefixed.exists():
            return prefixed
        name = prefixed.name
    stem, ext = os.path.splitext(name)
    n = 2
    while (dest / f"{stem} ({n}){ext}").exists():
        n += 1
    return dest / f"{stem} ({n}){ext}"


def _target_dir(dest: Path, section_name: str, organize: bool) -> Path:
    if not organize:
        return dest
    return dest / MOODLE_DIR / (download.safe_filename(section_name, max_bytes=desktop.MAX_FOLDER_BYTES) or "Section")


def _updated_name(name: str) -> str:
    stem, ext = os.path.splitext(name)
    return f"{stem} (updated {datetime.date.today().isoformat()}){ext}"


def _drm_placeholder(item: catalog.Item, content: bytes, name: str) -> tuple[bytes, str]:
    """A Moodle Book chapter that's really a DRM video player embed
    (VdoCipher, so far) can't be saved — see download.detect_drm_video_embed.
    Save a pointer to it instead of a dead HTML file that looks like a
    saved video."""
    title = download.extract_html_title(content, fallback=name)
    url = f"{moodle_client.MOODLE_URL}/mod/{item.modname}/view.php?id={item.module_id}"
    text = (
        "[DRM-protected video — not downloadable]\n\n"
        f"{title}\n\n"
        "Hosted via an encrypted/OTP-authenticated streaming player — there's no way to save a\n"
        "working local copy, by the provider's own design.\n\n"
        f"View it live on Moodle: {url}\n"
    )
    return text.encode("utf-8"), os.path.splitext(name)[0] + ".txt"


def _job(item: catalog.Item) -> browser.Job | None:
    """The browser job that fetches this item, or None when plain HTTP will do."""
    if item.kind == "watermarked":
        return browser.Job(item.key, "watermarked", f"{moodle_client.MOODLE_URL}/mod/filewithwatermark/view.php?id={item.module_id}")
    if item.kind == "link":
        kind = links.classify(item.fileurl).kind
        if kind in ("sharepoint", "webpage"):
            return browser.Job(item.key, kind, item.fileurl, name=item.name)
    return None


def _pending_login(course_id: int, item: catalog.Item, job: browser.Job) -> dict:
    info = {"courseId": course_id, "key": item.key, "name": item.name, "kind": job.kind, "url": job.url}
    if job.kind == "sharepoint":
        t = links.classify(job.url)
        info.update(downloadUrl=links.sharepoint_download_url(job.url, t.path), filename=t.filename)
    return info


def _fetch_browser(jobs: list[browser.Job], errors: list[str]) -> browser.Outcome:
    """One headless browser session for every job passed in. Public pages never
    need a login; logged-in jobs try the saved session and, if it's stale, come
    back in `needs_login` for the extension to handle."""
    out = browser.Outcome()
    if not jobs:
        return out
    try:
        out.merge(browser.fetch_public(jobs))
    except Exception as e:  # noqa: BLE001 — a browser problem shouldn't sink the regular downloads
        errors.append(f"Saving web pages: {e}")
    login_jobs = browser.needs_login_jobs(jobs)
    if login_jobs:
        try:
            out.merge(browser.fetch_all(login_jobs))
        except Exception as e:  # noqa: BLE001
            errors.append(f"Locked files: {e}")
    return out


def _record(course_id: int, item: catalog.Item, saved_as: str, size: int, is_update: bool) -> None:
    now = datetime.datetime.now().isoformat(timespec="seconds")

    def change(course: dict) -> None:
        course["downloaded"][item.key] = {
            "size": size,
            "timemodified": item.timemodified,
            "saved_as": saved_as,
            "at": now,
        }
        if is_update:
            course["flags"][item.key] = {"name": item.name, "saved_as": saved_as, "at": now}

    state.update_course(course_id, change)


def _pending(sections: list[catalog.Section], cfg: dict) -> list[tuple[catalog.Item, bool, str]]:
    """(item, is_update, section name) for everything ticked that's new or changed upstream.
    Locked files and links have no change signal, so they're fetched once."""
    todo = []
    for sec in sections:
        for item in sec.items:
            if not is_selected(item, sec.id, cfg["selection"], sec.parent):
                continue
            rec = cfg["downloaded"].get(item.key)
            if rec is None:
                todo.append((item, False, sec.name))
            elif item.kind not in ("watermarked", "link") and (rec["size"], rec["timemodified"]) != (item.size, item.timemodified):
                todo.append((item, True, sec.name))
    return todo


@dataclass
class _Plan:
    summary: Summary
    dest: Path | None = None
    organize: bool = True
    todo: list = field(default_factory=list)

    @property
    def jobs(self) -> list[browser.Job]:
        return [job for item, _, _ in self.todo if (job := _job(item))]


def busy() -> bool:
    """Is any course being synced or saved to right now? (An update must not
    restart the app in the middle of that.)"""
    with _locks_guard:
        return any(lock.locked() for lock in _locks.values())


def _make_plan(course_id: int, only: str = "") -> _Plan:
    """What would a sync do right now? Reads Moodle, touches no files.
    `only` narrows it to one item."""
    cfg = state.get_course(course_id)
    summary = Summary(course_id, cfg["name"] or str(course_id))
    plan = _Plan(summary)

    if not cfg["folder"]:
        summary.skipped = "no folder chosen"
        return plan
    if not state.has_selection(cfg["selection"]):
        summary.skipped = "nothing ticked"
        return plan
    try:
        sections = catalog.fetch(course_id)
    except (moodle_client.NoTokenError, moodle_client.InvalidTokenError):
        summary.errors.append("Not logged in to Moodle (or the login expired) — open LMS Sync and log in.")
        summary.auth_error = True
        return plan
    except Exception as e:  # noqa: BLE001
        summary.errors.append(f"Couldn't read the course from Moodle: {e}")
        return plan

    plan.dest = Path(cfg["folder"])
    plan.organize = cfg["organize"]
    course_folder.prepare(plan.dest, cfg["name"], course_id)
    plan.todo = [t for t in _pending(sections, cfg) if not only or t[0].key == only]
    return plan


def _run_plan(course_id: int, plan: _Plan, fetched: browser.Outcome) -> None:
    summary = plan.summary
    if plan.dest is None:
        return

    for item, is_update, section_name in plan.todo:
        try:
            job = _job(item)
            if job:  # fetched in the browser step
                if item.key in fetched.results:
                    filename, content = fetched.results[item.key]
                    name, expected = download.safe_filename(filename), None
                elif job.kind == "webpage":  # couldn't capture the page — at least keep the link
                    reason = fetched.errors.get(item.key, "unknown reason")
                    summary.errors.append(f"{item.name}: couldn't save the page ({reason}) — saved a link shortcut instead")
                    suffix, content = desktop.shortcut(item.fileurl)
                    name, expected = download.safe_filename(item.name) + suffix, None
                else:  # needs a login that isn't available yet
                    if item.key in fetched.errors:
                        summary.errors.append(f"{item.name}: {fetched.errors[item.key]}")
                    continue
            elif item.kind == "link":  # a direct link to a public file
                filename, content = links.download_direct(links.classify(item.fileurl).download_url or item.fileurl)
                name, expected = filename, None
            elif item.kind == "submission-text":
                content, name, expected = item.inline.encode("utf-8"), item.name, None
            else:
                content, name, expected = moodle_client.download_via_wstoken(item.fileurl), item.name, item.size

            allow_html = name.lower().endswith((".html", ".htm"))
            if allow_html and item.kind == "file" and download.detect_drm_video_embed(content):
                content, name = _drm_placeholder(item, content, name)
                allow_html, expected = False, None
            elif allow_html and item.kind == "file" and browser.is_converted_pdf(content):
                # A PDF uploaded as a web page: save it as a PDF again.
                try:
                    content, name = browser.html_pages_to_pdf(content), os.path.splitext(name)[0] + ".pdf"
                    allow_html, expected = False, None
                except Exception as e:  # noqa: BLE001 — the page itself is still worth having
                    summary.errors.append(f"{item.name}: couldn't turn it into a PDF ({str(e).splitlines()[0][:150]}) — saved the web page instead")

            folder = _target_dir(plan.dest, section_name, plan.organize)
            target = _unique_path(folder, _updated_name(name) if is_update else name, item.module_name)
            download.place_atomically(content, target, expected_size=expected, allow_html=allow_html)
            # Moodle's own size is what later syncs compare against — except
            # locked files and links, which have no size to compare and are fetched once.
            _record(course_id, item, target.name, len(content) if item.kind in ("watermarked", "link") else item.size, is_update)
            (summary.updated if is_update else summary.new).append(target.name)
        except (moodle_client.NoTokenError, moodle_client.InvalidTokenError):
            summary.errors.append("Moodle login expired mid-download — log in again and re-run.")
            summary.auth_error = True
            break
        except Exception as e:  # noqa: BLE001 — one bad file must not abort the rest
            summary.errors.append(f"{item.name}: {e}")

    waiting = [(item, job) for item, _, _ in plan.todo if (job := _job(item)) and job.kind in browser.NEEDS_LOGIN_KINDS
               and job.key not in fetched.results]
    summary.needs_login = len(waiting)
    summary.pending_login = [_pending_login(course_id, item, job) for item, job in waiting]
    # Remembered so the UI can say "N locked files are waiting for a login"
    # without re-reading Moodle for every course.
    state.update_course(course_id, lambda c: c.__setitem__("locked_waiting", summary.needs_login))


def save_link_file(course_id: int, item_key: str, filename: str, content: bytes) -> dict:
    """File `content` as the saved version of a link or locked item — the common
    ending of every manual route (the Chrome extension, Import file…)."""
    with _course_lock(course_id):
        cfg = state.get_course(course_id)
        if not cfg["folder"]:
            raise ValueError("Choose a folder for this course first.")
        found = next(
            ((item, sec) for sec in catalog.fetch(course_id) for item in sec.items if item.key == item_key and item.kind in ("link", "watermarked")),
            None,
        )
        if found is None:
            raise LookupError("That link isn't in this course any more.")
        item, section = found
        dest = Path(cfg["folder"])
        course_folder.prepare(dest, cfg["name"], course_id)
        target = _unique_path(_target_dir(dest, section.name, cfg["organize"]), download.safe_filename(filename), item.module_name)
        download.place_atomically(content, target, allow_html=target.suffix.lower() in (".html", ".htm", ".mhtml"))
        _record(course_id, item, target.name, len(content), False)
        if item.kind == "watermarked" or links.needs_login(item.fileurl):
            state.update_course(course_id, lambda c: c.__setitem__("locked_waiting", max(0, c["locked_waiting"] - 1)))
        return {"saved_as": target.name}


EXTRA_DIR = "Saved from browser"


def save_extra_file(course_id: int, filename: str, content: bytes) -> dict:
    """A file the user chose to add to this course from anywhere in Chrome (the
    extension's course list). Not a Moodle item, so nothing is tracked in state —
    it just lands in <course folder>/Saved from browser/."""
    with _course_lock(course_id):
        cfg = state.get_course(course_id)
        if not cfg["folder"]:
            raise ValueError("Choose a folder for this course first.")
        dest = Path(cfg["folder"])
        course_folder.prepare(dest, cfg["name"], course_id)
        target = _unique_path(dest / EXTRA_DIR, download.safe_filename(filename), "")
        download.place_atomically(content, target, allow_html=target.suffix.lower() in (".html", ".htm", ".mhtml"))
        return {"saved_as": target.name, "folder": f"{EXTRA_DIR}/"}


def _course_files_named(folder: Path, name: str) -> list[Path]:
    """Files called `name` anywhere under the course folder (the user may have
    moved it), skipping our own Claude/ notes folder."""
    found = []
    for root, dirs, files in os.walk(folder):
        if Path(root) == folder and "Claude" in dirs:
            dirs.remove("Claude")
        if name in files:  # equality, not a glob: names can contain [ ] * ?
            found.append(Path(root) / name)
    return sorted(found)


def locate_download(course_id: int, item_key: str) -> dict:
    """Where is the file we saved for this item right now? Never touches disk beyond reading."""
    cfg = state.get_course(course_id)
    rec = cfg["downloaded"].get(item_key)
    if not rec:
        raise LookupError("This item hasn't been downloaded.")
    folder = Path(cfg["folder"]) if cfg["folder"] else None
    found = _course_files_named(folder, rec["saved_as"]) if folder and folder.is_dir() else []
    return {"name": rec["saved_as"], "found": [p.relative_to(folder).as_posix() for p in found]}


# Document types it's safe to hand to their default app. Anything else (a
# script, an installer, an unknown type — it came off the internet and carries
# no quarantine flag) is only revealed in Finder, never launched.
OPENABLE = {
    ".pdf", ".doc", ".docx", ".rtf", ".txt", ".md", ".xls", ".xlsx", ".csv", ".ppt", ".pptx", ".pages", ".numbers",
    ".key", ".png", ".jpg", ".jpeg", ".gif", ".heic", ".mp3", ".m4a", ".mp4", ".mov", ".zip", ".webloc", ".url", ".html",
    ".htm", ".mhtml", ".json",
}


def open_download(course_id: int, item_key: str) -> dict:
    """Open the saved file for an item in its default app, wherever in the course
    folder it is now."""
    info = locate_download(course_id, item_key)
    if not info["found"]:
        raise LookupError(f"“{info['name']}” isn't in the course folder any more (moved elsewhere, renamed or deleted).")
    folder = Path(state.get_course(course_id)["folder"])
    path = max((folder / rel for rel in info["found"]), key=lambda p: p.stat().st_mtime)
    safe = path.suffix.lower() in OPENABLE and (path.suffix.lower() != ".url" or desktop.is_web_shortcut(path))
    if safe:
        desktop.open_path(str(path))
        return {"opened": path.relative_to(folder).as_posix(), "revealed": False}
    desktop.reveal(str(path))
    return {"opened": path.relative_to(folder).as_posix(), "revealed": True}


def open_course_folder(course_id: int) -> dict:
    """The course folder itself (the one holding Moodle/ and Claude/), in Finder."""
    folder = state.get_course(course_id)["folder"]
    if not folder:
        raise ValueError("Choose a folder for this course first.")
    if not Path(folder).is_dir():
        raise LookupError("That folder doesn't exist any more — choose it again.")
    desktop.open_path(folder)
    return {"opened": folder}


def forget_download(course_id: int, item_key: str, trash_rel: str | None) -> dict:
    """"Delete & re-download": optionally move the saved file to the Trash, and
    forget the item was ever downloaded so the next sync fetches it again.
    `trash_rel` must be the single file locate_download found — never a path the
    caller invents."""
    with _course_lock(course_id):
        info = locate_download(course_id, item_key)
        trashed = None
        if trash_rel is not None:
            if info["found"] != [trash_rel]:
                raise ValueError("The file changed since it was looked up — try again.")
            desktop.move_to_trash(str(Path(state.get_course(course_id)["folder"]) / trash_rel))
            trashed = trash_rel

        def forget(cfg: dict) -> None:
            cfg["downloaded"].pop(item_key, None)
            cfg["flags"].pop(item_key, None)

        state.update_course(course_id, forget)
        return {"trashed": trashed}


def import_file(course_id: int, item_key: str, source: Path) -> dict:
    """The user saved the file themselves. A copy, never a move: it's their file."""
    if not source.is_file():
        raise ValueError("That file doesn't exist.")
    return save_link_file(course_id, item_key, source.name, source.read_bytes())


def sync_course(course_id: int) -> Summary:
    with _course_lock(course_id):
        plan = _make_plan(course_id)
        fetched = _fetch_browser(plan.jobs, plan.summary.errors)
        _run_plan(course_id, plan, fetched)
        return plan.summary


def fetch_item(course_id: int, item_key: str) -> Summary:
    """"Download" on one file: tick it (so later changes keep arriving) and
    fetch just that one now."""
    with _course_lock(course_id):
        def tick(cfg: dict) -> None:
            sel = cfg["selection"]
            sel["excluded"] = [k for k in sel["excluded"] if k != item_key]
            if item_key not in sel["items"]:
                sel["items"] = sorted([*sel["items"], item_key])

        if not state.get_course(course_id)["folder"]:
            raise ValueError("Choose a folder for this course first (Files tab).")
        state.update_course(course_id, tick)
        plan = _make_plan(course_id, only=item_key)
        fetched = _fetch_browser(plan.jobs, plan.summary.errors)
        waiting = state.get_course(course_id)["locked_waiting"]
        _run_plan(course_id, plan, fetched)
        # _run_plan counts only what it was asked for; keep the course-wide number honest.
        state.update_course(course_id, lambda c: c.__setitem__("locked_waiting", max(waiting, plan.summary.needs_login)))
        return plan.summary


def sync_all() -> list[Summary]:
    """Every course in the syncing list, sharing one headless browser session."""
    ids = sorted(cid for cid, cfg in state.all_courses().items() if state.is_syncing(cfg) and state.has_selection(cfg["selection"]))
    with ExitStack() as stack:
        for cid in ids:  # sorted: every caller takes the locks in the same order
            stack.enter_context(_course_lock(cid))
        plans = {cid: _make_plan(cid) for cid in ids}
        jobs = [job for plan in plans.values() for job in plan.jobs]
        shared_errors: list[str] = []
        fetched = _fetch_browser(jobs, shared_errors)
        for cid, plan in plans.items():
            _run_plan(cid, plan, fetched)
        if shared_errors and plans:
            plans[ids[0]].summary.errors.extend(shared_errors)
        return [plan.summary for plan in plans.values()]
