#!/usr/bin/env python3
"""lms-sync — pick which of your ISB Moodle courses/sections/files to keep,
and where; files download flat into each course's folder.

Usage:
    python3 cli.py ui [--port N] [--no-open]   # the browser UI (where you pick things)
    python3 cli.py sync [course_id ...]        # download what's ticked (all ticked courses if none given)
    python3 cli.py update                      # install the newest released version
    python3 cli.py daily                       # what the daily scheduled job runs: sync + notify, never prompts
    python3 cli.py list-courses
    python3 cli.py status                      # what's set up, anything flagged
    python3 cli.py login                       # opens the app to log in (in your own Chrome)
"""
from __future__ import annotations

import argparse
import datetime
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

import board
import desktop
import engine
import moodle_client
import server
import state
import updater


def _print_summary(s: engine.Summary) -> None:
    if s.skipped:
        print(f"[{s.label}] skipped — {s.skipped}")
        return
    print(f"[{s.label}] +{len(s.new)} new, ~{len(s.updated)} updated")
    for name in s.new:
        print(f"  + {name}")
    for name in s.updated:
        print(f"  ~ {name}")
    if s.needs_login:
        print(f"  ! {s.needs_login} file(s) need a sign-in — open LMS Sync and they're fetched through your own Chrome (extension)")
    for err in s.errors:
        print(f"  x {err}")


def cmd_ui(args) -> None:
    server.run(port=args.port, open_browser=not args.no_open)


def cmd_sync(args) -> list[engine.Summary]:
    ids = [int(i) for i in args.course_ids] or [
        cid for cid, cfg in state.all_courses().items() if cfg["folder"] and state.has_selection(cfg["selection"])
    ]
    if not ids:
        print("Nothing to sync yet — open the UI (`cli.py ui`), choose a folder for a course and tick what you want.")
        return []
    summaries = [engine.sync_course(cid) for cid in ids]
    for s in summaries:
        _print_summary(s)
    return summaries


def cmd_list_courses(_args) -> None:
    for c in sorted(moodle_client.list_courses(), key=lambda c: c.startdate):
        date = datetime.datetime.fromtimestamp(c.startdate).strftime("%Y-%m-%d") if c.startdate else "-"
        print(f"{c.id:>6}  {date}  {server.term_of(c.shortname):<7} {c.fullname}")


def cmd_status(_args) -> None:
    configured = {cid: cfg for cid, cfg in state.all_courses().items() if cfg["folder"]}
    if not configured:
        print("No courses set up yet — open the UI (`cli.py ui`).")
        return
    for cid, cfg in configured.items():
        sel = cfg["selection"]
        what = "whole course" if sel["course"] else f"{len(sel['sections'])} section(s), {len(sel['items'])} file(s)"
        print(f"{cfg['name'] or cid}  [{cid}]\n  folder: {cfg['folder']}\n  ticked: {what}; {len(cfg['downloaded'])} downloaded")
        for flag in cfg["flags"].values():
            print(f"    ~ '{flag['name']}' changed on Moodle — new copy saved as '{flag['saved_as']}'")


def cmd_login(_args) -> None:
    """Logging in happens in the app, in the user's own browser."""
    print("Press “Log in to Moodle” on the page (needs the LMS Sync Chrome extension).")
    server.run(server.DEFAULT_PORT, open_browser=True)


def _wait_for_network(attempts: int = 6, delay_s: int = 15) -> bool:
    """launchd firing right as the Mac wakes can beat Wi-Fi reconnecting,
    which otherwise turns into every course failing with a bare connection
    error. A short bounded wait covers that without becoming an open-ended
    retry loop."""
    for attempt in range(attempts):
        try:
            requests.head(moodle_client.MOODLE_URL, timeout=5)
            return True
        except requests.exceptions.RequestException:
            if attempt < attempts - 1:
                time.sleep(delay_s)
    return False


def cmd_install_integration(args) -> None:
    """Desktop shortcut + daily schedule (the installers run this)."""
    root = moodle_client.PROJECT_ROOT
    steps = [("Desktop shortcut", lambda: desktop.create_shortcut(root / "src" / "launcher.py"))]
    if not args.no_daily:
        steps.append((f"Daily sync at {desktop.DAILY_HOUR}:00",
                      lambda: desktop.schedule_daily(root / "src" / "cli.py", root / "logs" / "daily.log")))
    failed = False
    for name, step in steps:
        try:
            step()
            print(f"  ✓ {name}")
        except (OSError, subprocess.SubprocessError) as e:
            failed = True
            print(f"  x {name} couldn't be set up: {e}")
    if failed:
        sys.exit(1)


def cmd_uninstall_integration(_args) -> None:
    desktop.remove_integration()
    print("Removed the desktop shortcut and the daily schedule. Delete this folder to remove the app and its data.")


def cmd_update(_args) -> None:
    try:
        version = updater.install()
    except updater.UpdateError as e:
        raise SystemExit(str(e))
    print(f"Updated to {version}. Reopen LMS Sync." if version else f"Already up to date ({updater.current_version()}).")


def cmd_daily(_args) -> None:
    """Entry point for the daily scheduled job: update if allowed, sync the
    courses in the syncing list, notify with a summary, never prompt, never raise."""
    def record(outcome: str, **counts) -> None:
        state.record_daily({"at": datetime.datetime.now().isoformat(timespec="seconds"), "outcome": outcome, **counts})

    if not _wait_for_network():
        record("offline")
        desktop.notify("LMS Sync Skipped", "No network after waiting ~90s — will try again tomorrow.")
        return
    if not os.environ.get("LMS_SYNC_UPDATED"):  # set on the re-run, so an update can't loop
        version = updater.install_if_automatic()
        if version:
            print(f"Updated to {version}")
            desktop.notify("LMS Sync updated", f"Now on version {version}.")
            # Run the rest of today's job with the new code, in a fresh process.
            subprocess.run([sys.executable, str(Path(__file__)), "daily"], env={**os.environ, "LMS_SYNC_UPDATED": "1"})
            return
    try:
        summaries = engine.sync_all()
    except Exception as e:  # noqa: BLE001 — an unattended job must not crash-loop silently
        record("failed", error=str(e)[:200])
        desktop.notify("LMS Sync Failed", str(e)[:200])
        return
    for s in summaries:
        _print_summary(s)

    new = sum(len(s.new) for s in summaries)
    updated = sum(len(s.updated) for s in summaries)
    locked = sum(s.needs_login for s in summaries)
    errors = sum(len(s.errors) for s in summaries)
    try:
        news = board.check_tracked()
    except Exception as e:  # noqa: BLE001 — announcements must never break the download report
        news = {"announcements": [], "due_soon": [], "problems": [str(e)]}
    for line in news["announcements"]:
        print(f"  announcement — {line}")
    for line in news["due_soon"]:
        print(f"  due within 48h — {line}")
    for line in news["problems"]:
        print(f"  x board: {line}")

    record("auth" if any(s.auth_error for s in summaries) else "ok",
           new=new, updated=updated, locked=locked, errors=errors, courses=len(summaries),
           announcements=len(news["announcements"]), due_soon=len(news["due_soon"]))
    if any(s.auth_error for s in summaries):
        desktop.notify("LMS Sync", "You're not logged in to Moodle (or the login expired) — open LMS Sync and log in.")
        return
    parts = []
    if new:
        parts.append(f"{new} new file(s)")
    if updated:
        parts.append(f"{updated} updated on Moodle")
    if locked:
        parts.append(f"{locked} file(s) need a sign-in — open LMS Sync")
    if errors:
        parts.append(f"{errors} error(s) — see the log")
    if news["announcements"]:
        parts.append(f"{len(news['announcements'])} new announcement(s)")
    if parts:
        desktop.notify("LMS Sync", ", ".join(parts))
    if news["due_soon"]:  # its own notification: a deadline shouldn't be buried in a file count
        more = len(news["due_soon"]) - 1
        desktop.notify("Due within 48 hours", news["due_soon"][0] + (f"  (+{more} more)" if more else ""))


def _set_up_output(command_line: list[str]) -> None:
    """Names and course titles aren't ASCII, and a scheduled job on Windows has
    no console at all (pythonw): print as UTF-8, or into the log."""
    if sys.stdout is None:
        log_dir = moodle_client.PROJECT_ROOT / "logs"
        log_dir.mkdir(exist_ok=True)
        name = "daily.log" if "daily" in command_line else "ui.log"
        sys.stdout = sys.stderr = open(log_dir / name, "a", encoding="utf-8", buffering=1)
        print(f"=== {datetime.datetime.now():%Y-%m-%d %H:%M:%S} ===")
    else:
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")


def main() -> None:
    _set_up_output(sys.argv[1:])
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ui")
    p.add_argument("--port", type=int, default=server.DEFAULT_PORT)
    p.add_argument("--no-open", action="store_true", help="don't open a browser tab")
    p.set_defaults(func=cmd_ui)

    p = sub.add_parser("sync")
    p.add_argument("course_ids", nargs="*")
    p.set_defaults(func=cmd_sync)

    sub.add_parser("daily").set_defaults(func=cmd_daily)
    sub.add_parser("update").set_defaults(func=cmd_update)
    p = sub.add_parser("install-integration")
    p.add_argument("--no-daily", action="store_true", help="shortcut only, no daily schedule")
    p.set_defaults(func=cmd_install_integration)
    sub.add_parser("uninstall-integration").set_defaults(func=cmd_uninstall_integration)
    sub.add_parser("list-courses").set_defaults(func=cmd_list_courses)
    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("login", aliases=["renew-token"]).set_defaults(func=cmd_login)

    args = parser.parse_args()
    try:
        args.func(args)
    except moodle_client.NoTokenError as e:
        print(f"\n{e}\n", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
