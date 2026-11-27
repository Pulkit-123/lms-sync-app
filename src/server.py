"""The browser UI's backend: a small localhost-only HTTP server (stdlib, no
extra dependencies) serving ui/index.html and a JSON API over catalog /
engine / state.

Localhost-only isn't enough on its own — any web page open in the browser
can fire requests at 127.0.0.1 — so every request is checked: the Host header
must be loopback (blocks DNS rebinding), and POSTs must be JSON from a
loopback Origin (a cross-site form post can't set either).
"""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import shutil
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import requests

import board
import browser
import catalog
import course_folder
import desktop
import download
import engine
import links
import moodle_client
import state
import updater

UI_PATH = Path(__file__).parent / "ui" / "index.html"
# Read once, so the page always matches the code that is running (an update
# replaces the files under us; the app then restarts itself).
UI_HTML = UI_PATH.read_text(encoding="utf-8")
RUNNING_VERSION = updater.current_version()
_restart = threading.Event()
DEFAULT_PORT = 8765
HOME = Path.home()
COURSES_ROOT = HOME / "ISB Courses"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost")

MAX_UPLOAD = 200 * 1024 * 1024
TOKEN_PATH = state.DATA_DIR / "extension_token"


def extension_token() -> str:
    """Pairs the Chrome extension with this server. Handed only to pages served
    from the loopback app itself (index.html), so a web page on another site
    can't read it (cross-origin reads of 127.0.0.1 are opaque, and the Host
    check blocks DNS rebinding)."""
    try:
        return TOKEN_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(24)
        TOKEN_PATH.write_text(token, encoding="utf-8")
        os.chmod(TOKEN_PATH, 0o600)
        return token


_capture_lock = threading.Lock()  # one visible capture window at a time (it owns the browser profile)
_courses_cache: dict = {"at": 0.0, "courses": []}


def _courses() -> list[moodle_client.Course]:
    if time.time() - _courses_cache["at"] > 60:
        _courses_cache["courses"] = moodle_client.list_courses()
        _courses_cache["at"] = time.time()
    return _courses_cache["courses"]


def _course(course_id: int) -> moodle_client.Course:
    for c in _courses():
        if c.id == course_id:
            return c
    raise LookupError(f"No course {course_id} in your Moodle enrolments.")


def term_of(shortname: str) -> str:
    last = shortname.split("_")[-1].strip()
    return last if re.fullmatch(r"T\d+|BW[\d.]+|IW|Pivot", last) else "Earlier"


def code_of(shortname: str) -> str:
    return re.sub(r"[^\w.-]", "", shortname.split("_")[0]) or shortname


def suggested_folder(c: moodle_client.Course) -> str:
    term = term_of(c.shortname)
    base = COURSES_ROOT / term if term != "Earlier" else COURSES_ROOT
    return str(base / code_of(c.shortname))


def _course_summary(c: moodle_client.Course, cfg: dict, now: float | None = None) -> dict:
    sel = cfg["selection"]
    return {
        "id": c.id,
        "name": download.clean_text(c.fullname),
        "code": code_of(c.shortname),
        "term": term_of(c.shortname),
        "startdate": c.startdate,
        "folder": cfg["folder"],
        "subscribed": state.has_selection(sel),
        "syncing": state.is_syncing(cfg),
        "sync_setting": cfg["syncing"],
        "ended": bool(c.enddate) and c.enddate < (now or time.time()),
        "enddate": c.enddate,
        "suggested_folder": suggested_folder(c),
        "flags": len(cfg["flags"]),
        "downloaded": len(cfg["downloaded"]),
        "locked_waiting": cfg["locked_waiting"],
        "organize": cfg["organize"],
        "in_extension": cfg["in_extension"],
        **board.counts(cfg["board"]),
    }


def _needs_login(item: catalog.Item) -> bool:
    return item.kind == "watermarked" or (item.kind == "link" and links.needs_login(item.fileurl))


def _target_info(item: catalog.Item) -> dict | None:
    if item.kind != "link":
        return None
    t = links.classify(item.fileurl)
    info = {"kind": t.kind, "filename": t.filename}
    if t.kind == "sharepoint":
        info["download_url"] = links.sharepoint_download_url(item.fileurl, t.path)
    elif t.kind == "file":
        info["download_url"] = t.download_url
    return info


def _item_status(item: catalog.Item, rec: dict | None) -> str:
    if rec is None:
        return "locked" if _needs_login(item) else "new"
    if item.kind not in ("watermarked", "link") and (rec["size"], rec["timemodified"]) != (item.size, item.timemodified):
        return "changed"
    return "downloaded"


def course_detail(course_id: int) -> dict:
    c = _course(course_id)
    cfg = state.get_course(course_id)
    sections = catalog.fetch(course_id)
    return {
        "course": _course_summary(c, cfg),
        "suggested_folder": suggested_folder(c),
        "selection": cfg["selection"],
        "flags": [{"key": k, **v} for k, v in cfg["flags"].items()],
        "sections": [
            {
                "id": s.id,
                "name": s.name,
                "parent": s.parent,
                "items": [
                    {
                        "key": i.key,
                        "name": i.name,
                        "note": i.note,
                        "size": i.size,
                        "timemodified": i.timemodified,
                        "kind": i.kind,
                        "url": i.fileurl if i.kind == "link" else "",
                        "target": _target_info(i),
                        "status": _item_status(i, cfg["downloaded"].get(i.key)),
                        "saved_as": (cfg["downloaded"].get(i.key) or {}).get("saved_as"),
                    }
                    for i in s.items
                ],
            }
            for s in sections
        ],
    }


def _remember_name(course_id: int) -> None:
    name = download.clean_text(_course(course_id).fullname)
    state.update_course(course_id, lambda c: c.__setitem__("name", name))


def set_folder(course_id: int, raw_path: str) -> dict:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise ValueError(f"Use a full path, like {COURSES_ROOT / 'T8' / 'PRMS'}")
    if HOME not in path.resolve().parents and path.resolve() != HOME:
        raise ValueError("The folder has to be somewhere inside your home folder.")
    name = download.clean_text(_course(course_id).fullname)  # fails fast on an unknown course, before touching disk
    path.mkdir(parents=True, exist_ok=True)
    course_folder.prepare(path, name, course_id)

    def change(cfg: dict) -> None:
        cfg["folder"] = str(path)
        cfg["name"] = name

    state.update_course(course_id, change)
    return {"folder": str(path)}


def set_syncing(course_id: int, value: bool) -> dict:
    """Put a course in (or take it out of) the daily sync. Turning it on needs a
    folder; with nothing ticked yet, it means the whole course. Turning it off
    keeps the folder, the ticks and every file."""
    c = _course(course_id)

    def change(cfg: dict) -> None:
        if value and not cfg["folder"]:
            raise ValueError("Choose a folder for this course first.")
        cfg["syncing"] = bool(value)
        if value and not state.has_selection(cfg["selection"]):
            cfg["selection"] = {"course": True, "sections": [], "items": [], "excluded": []}
        cfg["name"] = download.clean_text(c.fullname)

    before = state.get_course(course_id)
    after = state.update_course(course_id, change)
    took_whole_course = value and not state.has_selection(before["selection"])
    return {**_course_summary(c, after), "took_whole_course": bool(took_whole_course)}


def set_organize(course_id: int, organize: bool) -> dict:
    _course(course_id)  # unknown course -> clear error
    state.update_course(course_id, lambda cfg: cfg.__setitem__("organize", bool(organize)))
    return {"organize": bool(organize)}


def set_in_extension(course_id: int, value: bool) -> dict:
    _course(course_id)
    state.update_course(course_id, lambda cfg: cfg.__setitem__("in_extension", bool(value)))
    return {"in_extension": bool(value)}


def set_selection(course_id: int, body: dict) -> dict:
    def strings(key: str) -> list[str]:
        return sorted({str(x) for x in body.get(key, [])})

    selection = {
        "course": bool(body.get("course")),
        "sections": strings("sections"),
        "items": strings("items"),
        "excluded": strings("excluded"),
    }
    name = download.clean_text(_course(course_id).fullname)

    def change(cfg: dict) -> None:
        cfg["selection"] = selection
        cfg["name"] = name

    state.update_course(course_id, change)
    return selection


def dismiss_flags(course_id: int, key: str | None) -> dict:
    def change(cfg: dict) -> None:
        if key:
            cfg["flags"].pop(key, None)
        else:
            cfg["flags"].clear()

    state.update_course(course_id, change)
    return {"ok": True}


def pick_folder(start: str | None) -> dict:
    path = desktop.pick_folder(start)
    return {"path": path} if path else {"cancelled": True}


def pick_file() -> dict:
    path = desktop.pick_file()
    return {"path": path} if path else {"cancelled": True}


def _board_action(course_id: int, action: str, body: dict) -> dict:
    """Everything the user can change on a course's announcements & deadlines tab."""
    if action == "read":
        ann = body.get("id")
        board.mark_read(course_id, None if ann is None else int(ann))
    elif action == "done":
        board.set_done(course_id, str(body.get("key", "")), bool(body.get("done")))
    elif action in ("add", "edit"):
        fields = (str(body.get("title", "")), int(body.get("due") or 0), str(body.get("notes", "")), body.get("links", ""))
        if action == "add":
            board.add_manual(course_id, *fields)
        else:
            board.edit_manual(course_id, str(body.get("key", "")), *fields)
    elif action == "remove":
        board.remove(course_id, str(body.get("key", "")))
    elif action == "dismiss":
        board.dismiss_suggestion(course_id, str(body.get("key", "")))
    else:
        raise LookupError("not found")
    return {"ok": True}


def calendar_view(refresh: bool) -> dict:
    problems: list[str] = []
    if refresh:
        for c in _courses():  # names for any course not remembered yet
            if state.is_syncing(state.get_course(c.id)):
                _remember_name(c.id)
        problems = board.check_tracked()["problems"]
    view = board.calendar()
    try:
        codes = {c.id: code_of(c.shortname) for c in _courses()}
    except Exception:  # noqa: BLE001 — offline: the remembered deadlines are still worth showing
        codes = {}
    for d in view["deadlines"]:
        d["code"] = codes.get(d["course_id"], d["course"][:8])
    return {**view, "problems": problems}


def log_out() -> dict:
    """Forget this computer's Moodle sign-in: the token and any session cookies
    saved by earlier versions. Course folders, ticks and files are kept."""
    moodle_client.clear_token()
    browser.COOKIES_PATH.unlink(missing_ok=True)
    shutil.rmtree(moodle_client.SESSION_DIR, ignore_errors=True)
    shutil.rmtree(moodle_client.PROJECT_ROOT / "scripts" / ".moodle_session", ignore_errors=True)  # from earlier versions
    _courses_cache.update(at=0.0, courses=[])
    return {"ok": True}


def version_info() -> dict:
    try:
        manifest = json.loads((moodle_client.PROJECT_ROOT / "extension" / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = {}
    return {
        "version": RUNNING_VERSION,
        "restart_needed": updater.current_version() != RUNNING_VERSION,  # updated underneath us
        "os": "windows" if desktop.WINDOWS else "mac",
        "extension_version": manifest.get("version", ""),
    }


def request_restart(httpd) -> None:
    """Restart into the code now on disk, once nothing is being downloaded."""
    if _restart.is_set():
        return
    _restart.set()

    def stop() -> None:
        time.sleep(0.5)  # let this request's reply go out
        while engine.busy():
            time.sleep(1)
        httpd.shutdown()

    threading.Thread(target=stop, daemon=True).start()


def token_status() -> dict:
    try:
        return {"state": "ok", "user": moodle_client.site_info().get("fullname", "")}
    except moodle_client.NoTokenError:
        return {"state": "missing"}
    except moodle_client.InvalidTokenError:
        return {"state": "expired"}
    except requests.exceptions.RequestException:
        return {"state": "offline"}


class Handler(BaseHTTPRequestHandler):
    server_version = "lms-sync"

    def log_message(self, fmt, *args):  # quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _is_extension_call(self) -> bool:
        return urlparse(self.path).path.startswith("/api/ext/")

    def _allowed(self) -> bool:
        if self.headers.get("Host", "").split(":")[0] not in LOOPBACK_HOSTS:
            return False
        if self._is_extension_call():
            # Only the paired extension: a chrome-extension:// origin (a web page can't
            # forge one) carrying the secret token.
            # (A GET from the extension carries no Origin header at all; any *web* origin is refused.)
            origin = self.headers.get("Origin") or ""
            origin_ok = origin.startswith("chrome-extension://") or (self.command == "GET" and origin == "")
            return origin_ok and hmac.compare_digest(self.headers.get("X-LMS-Token", ""), extension_token())
        if self.command == "POST":
            origin = urlparse(self.headers.get("Origin") or "http://127.0.0.1").hostname
            if origin not in LOOPBACK_HOSTS or "application/json" not in self.headers.get("Content-Type", ""):
                return False
        return True

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def _dispatch(self) -> None:
        if not self._allowed():
            return self._json({"error": "forbidden"}, 403)
        path = urlparse(self.path).path
        try:
            return self._route(path)
        except (moodle_client.NoTokenError, moodle_client.InvalidTokenError):
            self._json({"error": "Moodle login expired", "code": "auth"}, 401)
        except (LookupError, ValueError) as e:
            self._json({"error": str(e)}, 400)
        except requests.exceptions.RequestException as e:
            self._json({"error": f"Can't reach Moodle: {e}", "code": "offline"}, 502)
        except Exception as e:  # noqa: BLE001
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def _route(self, path: str) -> None:
        get, post = self.command == "GET", self.command == "POST"
        if get and path in ("/", "/index.html"):
            page = UI_HTML.replace("<!--LMS_TOKEN-->", f'<meta name="lms-token" content="{extension_token()}">')
            return self._send(200, page.encode(), "text/html; charset=utf-8")
        if get and path == "/api/ping":
            return self._json({"ok": True})
        if get and path == "/api/status":
            return self._json({**token_status(), **version_info(), "last_daily": state.last_daily(),
                               "extension_dir": str(moodle_client.PROJECT_ROOT / "extension")})
        if get and path == "/api/courses":
            configured = state.all_courses()
            blank = state.get_course(-1)
            return self._json([_course_summary(c, configured.get(c.id, blank)) for c in _courses()])
        if get and path == "/api/ext/courses":
            configured = state.all_courses()
            return self._json([
                {"id": c.id, "name": download.clean_text(c.fullname), "code": code_of(c.shortname), "term": term_of(c.shortname)}
                for c in _courses()
                if configured.get(c.id, {}).get("folder") and configured[c.id]["in_extension"]
            ])
        if post and path == "/api/ext/add":
            q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_UPLOAD:
                raise ValueError("Empty or too large.")
            return self._json(engine.save_extra_file(int(q["course"]), unquote(q.get("filename", "")) or "page.pdf", self.rfile.read(length)))
        if post and path == "/api/ext/token":
            info = moodle_client.save_token(str(self._body().get("wstoken", "")))
            _courses_cache["at"] = 0.0
            return self._json({"ok": True, "user": info.get("fullname", "")})
        if post and path == "/api/ext/upload":
            q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_UPLOAD:
                raise ValueError("Empty or too large.")
            content = self.rfile.read(length)
            return self._json(engine.save_link_file(int(q["course"]), q["key"], unquote(q.get("filename", "")) or "page.pdf", content))
        if get and path == "/api/calendar":
            return self._json(calendar_view(refresh=False))
        if post and path == "/api/calendar/refresh":
            return self._json(calendar_view(refresh=True))
        if get and path == "/api/update":
            return self._json(updater.check())
        if post and path == "/api/update/check":
            return self._json(updater.check(force=True))
        if post and path == "/api/update/auto":
            updater.set_auto(bool(self._body().get("value")))
            return self._json(updater.check())
        if post and path == "/api/update/install":
            if engine.busy():
                raise ValueError("A download is running — update when it finishes.")
            try:
                version = updater.install()
            except updater.UpdateError as e:
                raise ValueError(str(e))
            if version:
                request_restart(self.server)
            return self._json({"updated_to": version})
        if post and path == "/api/restart":
            request_restart(self.server)
            return self._json({"ok": True})
        if post and path == "/api/logout":
            return self._json(log_out())
        if post and path == "/api/open-extension-folder":
            desktop.open_path(str(moodle_client.PROJECT_ROOT / "extension"))
            return self._json({"ok": True})
        if post and path == "/api/pick-folder":
            return self._json(pick_folder(self._body().get("start")))
        if post and path == "/api/sync-all":
            return self._json([s.to_dict() for s in engine.sync_all()])

        m = re.fullmatch(r"/api/course/(\d+)(/[a-z/-]+)?", path)
        if m:
            cid, sub = int(m.group(1)), m.group(2) or ""
            if get and sub == "":
                return self._json(course_detail(cid))
            if post and sub == "/folder":
                return self._json(set_folder(cid, self._body().get("path", "")))
            if post and sub == "/fetch":
                return self._json(engine.fetch_item(cid, str(self._body().get("key", ""))).to_dict())
            if post and sub == "/open":
                key = self._body().get("key")
                return self._json(engine.open_download(cid, str(key)) if key else engine.open_course_folder(cid))
            if get and sub == "/board":
                _course(cid)  # unknown course -> clear error
                _remember_name(cid)
                return self._json(board.refresh(cid))
            if post and sub.startswith("/board/"):
                return self._json(_board_action(cid, sub.removeprefix("/board/"), self._body()))
            if post and sub == "/redownload":
                body = self._body()
                if not body.get("confirm"):
                    return self._json(engine.locate_download(cid, str(body.get("key", ""))))
                return self._json(engine.forget_download(cid, str(body.get("key", "")), body.get("trash")))
            if post and sub == "/syncing":
                return self._json(set_syncing(cid, bool(self._body().get("value"))))
            if post and sub == "/in-extension":
                return self._json(set_in_extension(cid, self._body().get("value", True)))
            if post and sub == "/organize":
                return self._json(set_organize(cid, self._body().get("organize", True)))
            if post and sub == "/selection":
                return self._json(set_selection(cid, self._body()))
            if post and sub == "/sync":
                return self._json(engine.sync_course(cid).to_dict())
            if post and sub == "/import":
                if not _capture_lock.acquire(blocking=False):
                    raise ValueError("A capture window is already open — finish or close it first.")
                try:
                    picked = pick_file()  # the path never comes from the page, only from the native dialog
                    if picked.get("cancelled"):
                        return self._json({"cancelled": True})
                    return self._json(engine.import_file(cid, str(self._body().get("key", "")), Path(picked["path"])))
                finally:
                    _capture_lock.release()
            if post and sub == "/flags/dismiss":
                return self._json(dismiss_flags(cid, self._body().get("key")))
        self._json({"error": "not found"}, 404)

    do_GET = _dispatch
    do_POST = _dispatch


def _already_running(port: int) -> bool:
    try:
        return requests.get(f"http://127.0.0.1:{port}/api/ping", timeout=1).ok
    except requests.exceptions.RequestException:
        return False


class Server(ThreadingHTTPServer):
    # On Windows, "reuse address" would let a second copy bind the same port
    # instead of noticing one is already running.
    allow_reuse_address = not desktop.WINDOWS


def run(port: int = DEFAULT_PORT, open_browser: bool = True) -> None:
    url = f"http://127.0.0.1:{port}/"
    try:
        httpd = Server(("127.0.0.1", port), Handler)
    except OSError:
        if _already_running(port):
            print(f"Already running — opening {url}")
            if open_browser:
                webbrowser.open(url)
            return
        raise SystemExit(f"Port {port} is in use by something else — try --port.")
    print(f"LMS Sync UI running at {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    httpd.server_close()
    if _restart.is_set():
        src = Path(__file__).parent
        desktop.spawn_detached([desktop.background_python(), str(src / "cli.py"), "ui", "--no-open", "--port", str(port)],
                               src, moodle_client.PROJECT_ROOT / "logs" / "ui.log")
