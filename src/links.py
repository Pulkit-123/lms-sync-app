"""What to do with a Moodle "URL" resource (an article, a SharePoint course
pack, a website used in class). Pure decisions plus one plain HTTP download;
the browser-driven captures live in browser.py.

  sharepoint  ISB's SharePoint file viewer link → the file itself, but only
              with a Microsoft sign-in (a visible browser login, like locked PDFs)
  file        a direct link to a document (.pdf, .xlsx …), or a Dropbox share link
              (rewritten to its real-file form) → plain download
  webpage     anything else → a PDF snapshot of the page, rendered headless
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse, urlunparse

import requests

import download

FILE_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".ppt", ".pptx", ".zip", ".txt", ".png", ".jpg", ".jpeg",
}


@dataclass
class Target:
    kind: str  # "sharepoint" | "file" | "webpage"
    filename: str = ""  # best guess at the saved name ("" = decide after fetching)
    path: str = ""  # sharepoint: server-relative path of the file
    download_url: str = ""  # file: the URL that serves the actual bytes (may differ from the link)


def _is_sharepoint(host: str) -> bool:
    return host.endswith(".sharepoint.com")


def _ext(path: str) -> str:
    return os.path.splitext(path)[1].lower()


def _dropbox_direct(parts) -> str:
    """A Dropbox share link (…?dl=0) serves its web preview; dl=1 serves the file
    (or a .zip for a shared folder)."""
    query = {k: v for k, v in parse_qs(parts.query).items() if k not in ("dl", "raw")}
    return urlunparse(parts._replace(query=urlencode({**{k: v[0] for k, v in query.items()}, "dl": "1"})))


def classify(url: str) -> Target:
    parts = urlparse(url)
    if parts.scheme not in ("http", "https"):
        return Target("webpage")
    if (parts.hostname or "") in ("www.dropbox.com", "dropbox.com"):
        return Target("file", filename=os.path.basename(unquote(parts.path)), download_url=_dropbox_direct(parts))
    if _is_sharepoint(parts.hostname or ""):
        # Library viewer links carry the file's path in ?id=…
        path = unquote((parse_qs(parts.query).get("id") or [""])[0])
        if _ext(path) in FILE_EXTENSIONS:
            return Target("sharepoint", filename=os.path.basename(path), path=path)
        return Target("webpage")
    if _ext(unquote(parts.path)) in FILE_EXTENSIONS:
        return Target("file", filename=os.path.basename(unquote(parts.path)), download_url=url)
    return Target("webpage")


def needs_login(url: str) -> bool:
    return classify(url).kind == "sharepoint"


def sharepoint_download_url(url: str, path: str) -> str:
    """The direct-download endpoint for a file in a SharePoint/OneDrive site
    (the site is the first two path segments: /sites/<name>, /teams/<name>…)."""
    parts = urlparse(url)
    site = "/" + "/".join(path.strip("/").split("/")[:2])
    return f"{parts.scheme}://{parts.netloc}{site}/_layouts/15/download.aspx?SourceUrl={quote(path, safe='')}"


def download_direct(url: str, timeout: int = 60) -> tuple[str, bytes]:
    """A plain public file link. Raises if what comes back is a web page."""
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    if "text/html" in r.headers.get("content-type", "") and _ext(urlparse(url).path) not in (".htm", ".html"):
        raise ValueError("the link returned a web page, not a file (it may need a sign-in)")
    disposition = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', r.headers.get("content-disposition", ""))
    name = unquote(disposition.group(1)) if disposition else os.path.basename(unquote(urlparse(url).path))
    return download.safe_filename(name or "download"), r.content
