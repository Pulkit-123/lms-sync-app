"""Headless browser work — never a visible window. Three kinds of job:

  watermarked  Moodle's `filewithwatermark` plugin: no webservice API, its
               URLs reject the wstoken, so it needs a logged-in Moodle session.
  sharepoint   A file on ISB's SharePoint (a Moodle link to a course pack or
               outline): needs a Microsoft sign-in.
  webpage      An article / website: saved as a PDF snapshot of the page.
               Public, so it never needs a login.

Anything that needs the user to sign in is NOT done here: the app hands it to
the Chrome extension, which fetches it inside the user's own Chrome (their
sign-ins, their passkeys, a site's human checks all work there). This module
only tries the session cookies left over from an earlier login
(data/moodle_session.json, Moodle + SharePoint only) and reports "needs login"
for whatever they no longer honour. Those cookies are no longer refreshed by
anything, so expect that to be the common outcome.

Files are fetched through the browser context's cookie-sharing HTTP client, not
by navigating a page: headless Chromium has no PDF viewer and turns a PDF
navigation into a download, which makes page.goto raise. Never touches a
password field.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse

import download
import links
import state
from moodle_client import MOODLE_URL, SESSION_DIR

COOKIES_PATH = state.DATA_DIR / "moodle_session.json"
LOGIN_MARKERS = ("login.microsoftonline.com", "/adfs/", "/login/index.php")
NEEDS_LOGIN_KINDS = ("watermarked", "sharepoint")


@dataclass
class Job:
    key: str  # the catalog item's key — results come back under it
    kind: str  # "watermarked" | "sharepoint" | "webpage"
    url: str
    name: str = ""  # webpage: what to call the PDF


@dataclass
class Outcome:
    results: dict[str, tuple[str, bytes]] = field(default_factory=dict)  # key -> (filename, content)
    needs_login: set[str] = field(default_factory=set)
    errors: dict[str, str] = field(default_factory=dict)

    def merge(self, other: "Outcome") -> None:
        self.results.update(other.results)
        self.needs_login |= other.needs_login
        self.errors.update(other.errors)
        self.needs_login -= set(self.results)


def needs_login_jobs(jobs: list[Job]) -> list[Job]:
    return [j for j in jobs if j.kind in NEEDS_LOGIN_KINDS]


def _is_login(url: str) -> bool:
    return any(m in url for m in LOGIN_MARKERS)


def _launch(p, headless: bool):
    """The user's installed Google Chrome when there is one (a real browser, so
    passkeys / Touch ID / iCloud Keychain sign-in can be offered), else
    Playwright's bundled Chromium. Persistent profile = SESSION_DIR."""
    try:
        return p.chromium.launch_persistent_context(str(SESSION_DIR), headless=headless, channel="chrome")
    except Exception:  # noqa: BLE001 — Chrome not installed
        return p.chromium.launch_persistent_context(str(SESSION_DIR), headless=headless)


def _restore_session(context) -> None:
    try:
        context.add_cookies(json.loads(COOKIES_PATH.read_text(encoding="utf-8")))
    except (FileNotFoundError, ValueError):
        pass


def _fetch_watermarked(context, jobs: list[Job], timeout_ms: int = 15000) -> Outcome:
    out = Outcome()
    expired = False
    for job in jobs:
        if expired:  # the session is stale — don't hammer Moodle once per file
            out.needs_login.add(job.key)
            continue
        try:
            response = context.request.get(job.url, timeout=timeout_ms)
        except Exception as e:  # noqa: BLE001
            out.errors[job.key] = str(e)
            continue
        if _is_login(response.url):
            expired = True
            out.needs_login.add(job.key)
            continue
        filename = unquote(response.url.split("/")[-1].split("?")[0]) or f"watermarked-{job.key}"
        out.results[job.key] = (filename, response.body())
    return out


def _fetch_sharepoint(context, job: Job, timeout_ms: int = 30000) -> Outcome:
    """Unattended: uses whatever session cookies the context has."""
    out = Outcome()
    target = links.classify(job.url)
    try:
        response = context.request.get(links.sharepoint_download_url(job.url, target.path), timeout=timeout_ms)
        if _is_login(response.url) or "text/html" in response.headers.get("content-type", ""):
            out.needs_login.add(job.key)
        else:
            out.results[job.key] = (download.safe_filename(target.filename), response.body())
    except Exception as e:  # noqa: BLE001
        out.errors[job.key] = str(e)
    return out


def _fetch_logged_in(context, jobs: list[Job]) -> Outcome:
    out = Outcome()
    out.merge(_fetch_watermarked(context, [j for j in jobs if j.kind == "watermarked"]))
    for job in jobs:
        if job.kind == "sharepoint":
            out.merge(_fetch_sharepoint(context, job))
    return out


def fetch_all(jobs: list[Job]) -> Outcome:
    """Unattended: no login attempt. Tries the last saved session; whatever it
    no longer honours comes back in `needs_login`."""
    from playwright.sync_api import sync_playwright

    jobs = needs_login_jobs(jobs)
    if not jobs:
        return Outcome()
    with sync_playwright() as p:
        context = _launch(p, headless=True)
        try:
            _restore_session(context)
            return _fetch_logged_in(context, jobs)
        finally:
            context.close()


def _launch_plain(p):
    try:
        return p.chromium.launch(headless=True, channel="chrome")
    except Exception:  # noqa: BLE001
        return p.chromium.launch(headless=True)


def fetch_public(jobs: list[Job], timeout_ms: int = 45000) -> Outcome:
    """Web pages → PDF snapshots. A fresh, cookie-less context each time: public
    sites don't need the user's sessions and shouldn't get them."""
    from playwright.sync_api import sync_playwright

    jobs = [j for j in jobs if j.kind == "webpage"]
    out = Outcome()
    if not jobs:
        return out
    with sync_playwright() as p:
        browser = _launch_plain(p)
        try:
            context = browser.new_context()
            for job in jobs:
                page = context.new_page()
                try:
                    response = page.goto(job.url, wait_until="load", timeout=timeout_ms)
                    try:
                        page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:  # noqa: BLE001 — chatty pages never go idle; the load event is enough
                        pass
                    if response is not None and response.status >= 400:
                        raise ValueError(f"the site answered {response.status}")
                    if _is_login(page.url):
                        raise ValueError("it needs a sign-in")
                    pdf = page.pdf(format="A4", print_background=True)
                    # Moodle often titles a link with its own URL — useless as a filename.
                    given = "" if job.name.lower().startswith(("http://", "https://")) else job.name
                    title = given or page.title() or (urlparse(job.url).hostname or "page")
                    out.results[job.key] = (download.safe_filename(title) + ".pdf", pdf)
                except Exception as e:  # noqa: BLE001
                    out.errors[job.key] = str(e).splitlines()[0][:200]
                finally:
                    page.close()
        finally:
            browser.close()
    return out
