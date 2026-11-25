"""Validate-then-atomically-place a downloaded file, so a truncated transfer
or an HTML login page saved with a .pdf name never ends up looking like a
successfully synced file."""
from __future__ import annotations

import html
import os
import re
import tempfile
import unicodedata
from pathlib import Path

import desktop

_HTML_MARKERS = (b"<html", b"<!DOCTYPE html", b"<!doctype html")

# Streaming/DRM video providers seen embedded in Moodle "Book" chapter
# exports so far — download prevention is these products' actual purpose
# (encrypted, OTP-token-authenticated playback), not an oversight to route
# around. Extend this tuple if another provider turns up; the fix is the
# same either way (see detect_drm_video_embed's docstring).
_DRM_VIDEO_MARKERS = (b"player.vdocipher.com",)

_ILLEGAL = re.compile(r'[\/\\:\*\?"<>\|\x00-\x1f]')
_TAGS = re.compile(r"<[^>]+>")


def clean_text(text: str) -> str:
    """Moodle names can carry HTML tags/entities; strip them for display."""
    return _TAGS.sub("", html.unescape(text or "")).strip()


# Names Windows reserves for devices, with any extension.
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def safe_filename(name: str, max_bytes: int = desktop.MAX_NAME_BYTES) -> str:
    """A filesystem-safe version of a name that keeps its extension intact
    even when truncating."""
    name = unicodedata.normalize("NFC", clean_text(name))
    name = _ILLEGAL.sub("-", name)
    name = re.sub(r"\s+", " ", name).strip(" .") or "unnamed"
    if name.split(".")[0].strip().lower() in _RESERVED:
        name = "_" + name
    stem, ext = os.path.splitext(name)
    if len(name.encode("utf-8")) > max_bytes:
        room = max(1, max_bytes - len(ext.encode("utf-8")))
        stem = stem.encode("utf-8")[:room].decode("utf-8", errors="ignore").rstrip(" .")
        name = (stem or "unnamed") + ext
    return name


def looks_like_html_error_page(content: bytes) -> bool:
    head = content[:512].lstrip().lower()
    return any(head.startswith(m.lower()) for m in _HTML_MARKERS)


def detect_drm_video_embed(content: bytes) -> bool:
    """True if this downloaded HTML is a DRM-protected video player embed
    rather than real, savable content. Saving the raw embed HTML as-is is
    actively worse than skipping it: the player URL carries a single-use/
    short-lived OTP token, so the saved file doesn't even work once
    reopened later — it just looks like a saved video without being one."""
    return any(marker in content[:4096] for marker in _DRM_VIDEO_MARKERS)


def extract_html_title(content: bytes, fallback: str) -> str:
    """Pulls the first <h1>/<h2>/<h3> heading out of a Moodle Book chapter
    export (that's the chapter title in every case seen so far) for a
    human-readable placeholder name; falls back to the original filename
    if nothing matches."""
    match = re.search(rb"<h[1-3][^>]*>(.*?)</h[1-3]>", content[:2048], re.IGNORECASE | re.DOTALL)
    if not match:
        return fallback
    text = re.sub(rb"<[^>]+>", b"", match.group(1)).strip()
    try:
        return text.decode("utf-8") or fallback
    except UnicodeDecodeError:
        return fallback


def place_atomically(
    content: bytes, dest_path: Path, expected_size: int | None = None, allow_html: bool = False
) -> None:
    """allow_html: the file is legitimately HTML (a Moodle page/book export
    or a text submission), so the "got a login page instead of a file"
    check doesn't apply."""
    if not content:
        raise ValueError(f"empty download for {dest_path.name}")
    if not allow_html and looks_like_html_error_page(content):
        raise ValueError(f"{dest_path.name}: got an HTML page instead of a file (session likely expired)")
    if expected_size and abs(len(content) - expected_size) > max(16, expected_size * 0.01):
        raise ValueError(f"{dest_path.name}: size mismatch (got {len(content)}, expected ~{expected_size})")

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dest_path.parent, suffix=".partial")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        os.replace(tmp_path, dest_path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
