"""Everything that differs between macOS and Windows: dialogs, notifications,
opening files, the Trash / Recycle Bin, file locking, link shortcut files and
starting a background process. Nothing else in the app asks which OS it's on.

(Linux gets reasonable fallbacks for most of these, but isn't supported.)
"""
from __future__ import annotations

import html
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

WINDOWS = sys.platform.startswith("win")
MAC = sys.platform == "darwin"

# Windows refuses paths beyond ~260 characters by default, so names are kept shorter there.
MAX_NAME_BYTES = 120 if WINDOWS else 200
MAX_FOLDER_BYTES = 80 if WINDOWS else 200

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _powershell(script: str, args: tuple[str, ...] = (), timeout: int = 600) -> subprocess.CompletedProcess:
    """Values travel through environment variables (LMS_ARG0…), never spliced
    into the script, so a file or course name can't become code."""
    env = {**os.environ, **{f"LMS_ARG{i}": a for i, a in enumerate(args)}}
    return subprocess.run(
        ["powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-Command",
         "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + script],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=timeout, creationflags=_NO_WINDOW,
    )


def _applescript_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


# ---- dialogs -------------------------------------------------------------

def pick_folder(start: str | None = None) -> str | None:
    """A native "choose a folder" dialog (a web page can't be handed a real
    path by its own picker). None if the user cancelled."""
    start_dir = str(Path(start).expanduser()) if start and Path(start).expanduser().is_dir() else ""
    if WINDOWS:
        result = _powershell(
            "Add-Type -AssemblyName System.Windows.Forms; $d = New-Object System.Windows.Forms.FolderBrowserDialog; "
            "$d.Description = \"Choose this course's folder\"; $d.ShowNewFolderButton = $true; "
            "if ($env:LMS_ARG0) { $d.SelectedPath = $env:LMS_ARG0 }; "
            "$o = New-Object System.Windows.Forms.Form -Property @{TopMost=$true}; "
            "if ($d.ShowDialog($o) -eq 'OK') { $d.SelectedPath }",
            (start_dir,),
        )
        return result.stdout.strip() or None
    default = f" default location (POSIX file {_applescript_string(start_dir)})" if start_dir else ""
    script = f'tell me to activate\nPOSIX path of (choose folder with prompt "Choose this course\'s folder"{default})'
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=600)
    return result.stdout.strip().rstrip("/") if result.returncode == 0 else None


def pick_file() -> str | None:
    """A native file dialog opened in Downloads (where a browser saves)."""
    downloads = Path.home() / "Downloads"
    start_dir = str(downloads) if downloads.is_dir() else ""
    if WINDOWS:
        result = _powershell(
            "Add-Type -AssemblyName System.Windows.Forms; $d = New-Object System.Windows.Forms.OpenFileDialog; "
            "$d.Title = 'Choose the file you saved'; if ($env:LMS_ARG0) { $d.InitialDirectory = $env:LMS_ARG0 }; "
            "$o = New-Object System.Windows.Forms.Form -Property @{TopMost=$true}; "
            "if ($d.ShowDialog($o) -eq 'OK') { $d.FileName }",
            (start_dir,),
        )
        return result.stdout.strip() or None
    default = f" default location (POSIX file {_applescript_string(start_dir)})" if start_dir else ""
    script = f'tell me to activate\nPOSIX path of (choose file with prompt "Choose the file you saved"{default})'
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=600)
    return result.stdout.strip() if result.returncode == 0 else None


# ---- notifications -------------------------------------------------------

def notify(title: str, message: str) -> None:
    """Best-effort desktop notification: background jobs must succeed or fail
    visibly, not only in a log file."""
    try:
        if WINDOWS:
            _powershell(
                "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
                "$n = New-Object System.Windows.Forms.NotifyIcon; $n.Icon = [System.Drawing.SystemIcons]::Information; "
                "$n.Visible = $true; $n.ShowBalloonTip(10000, $env:LMS_ARG0, $env:LMS_ARG1, 'Info'); "
                "Start-Sleep -Seconds 6; $n.Dispose()",
                (title, message[:250]), timeout=20,
            )
        elif MAC:
            script = f"display notification {_applescript_string(message)} with title {_applescript_string(title)}"
            subprocess.run(["osascript", "-e", script], check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


# ---- opening and removing files -------------------------------------------

def open_path(path: str) -> None:
    """Open a file or folder the way double-clicking it would."""
    if WINDOWS:
        os.startfile(path)  # type: ignore[attr-defined]  # Windows only
    else:
        subprocess.run(["open" if MAC else "xdg-open", path], check=True, capture_output=True, timeout=30)


def reveal(path: str) -> None:
    """Show a file in Explorer / Finder without opening it."""
    if WINDOWS:
        subprocess.run(["explorer", f"/select,{os.path.normpath(path)}"], check=False, timeout=30)
    elif MAC:
        subprocess.run(["open", "-R", path], check=True, capture_output=True, timeout=30)
    else:
        open_path(str(Path(path).parent))


def move_to_trash(path: str) -> None:
    """Into the Trash / Recycle Bin (recoverable), never a permanent delete."""
    from send2trash import send2trash

    send2trash(path)


# ---- link shortcut files ----------------------------------------------------

SHORTCUT_SUFFIXES = (".webloc", ".url")


def shortcut(url: str) -> tuple[str, bytes]:
    """(suffix, content) of a double-clickable shortcut to a web page."""
    if WINDOWS:
        clean = url.replace("\r", "").replace("\n", "")
        return ".url", f"[InternetShortcut]\r\nURL={clean}\r\n".encode("utf-8")
    return ".webloc", (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        f'<plist version="1.0"><dict><key>URL</key><string>{html.escape(url)}</string></dict></plist>\n'
    ).encode("utf-8")


def is_web_shortcut(path: Path) -> bool:
    """A .url file can point at a local program; only a plain web one is safe to open."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    urls = [line[4:].strip().lower() for line in lines if line.lower().startswith("url=")]
    return len(urls) == 1 and urls[0].startswith(("http://", "https://"))


# ---- processes and locks -----------------------------------------------------

@contextmanager
def file_lock(path: Path):
    """Exclusive lock shared between processes (the app and the daily job)."""
    with open(path, "a+b") as handle:
        if WINDOWS:
            import msvcrt

            handle.seek(0)
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)  # waits ~10s per attempt
                    break
                except OSError:
                    continue
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def background_python() -> str:
    """The interpreter for a process with no window (pythonw on Windows)."""
    if WINDOWS:
        quiet = Path(sys.executable).with_name("pythonw.exe")
        if quiet.exists():
            return str(quiet)
    return sys.executable


def spawn_detached(args: list[str], cwd: Path, log: Path) -> None:
    """Start a process that outlives this one, its output appended to `log`."""
    log.parent.mkdir(parents=True, exist_ok=True)
    extra = (
        {"creationflags": getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
        if WINDOWS else {"start_new_session": True}
    )
    with open(log, "ab") as out:
        subprocess.Popen(args, cwd=cwd, stdin=subprocess.DEVNULL, stdout=out, stderr=out, **extra)


# ---- desktop shortcut and the daily schedule (used by the installers) --------

TASK_NAME = "LMS Sync daily"
AGENT_LABEL = "com.lmssync.daily"
DAILY_HOUR = 8


def create_shortcut(launcher: Path) -> Path:
    """A double-clickable "LMS Sync" on the Desktop."""
    if WINDOWS:
        result = _powershell(
            "$ErrorActionPreference='Stop'; "
            "$p = Join-Path ([Environment]::GetFolderPath('Desktop')) 'LMS Sync.lnk'; "
            "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($p); "
            "$s.TargetPath = $env:LMS_ARG0; $s.Arguments = '\"' + $env:LMS_ARG1 + '\"'; "
            "$s.WorkingDirectory = $env:LMS_ARG2; $s.Description = 'Open LMS Sync'; $s.Save(); $p",
            (background_python(), str(launcher), str(launcher.parent)),
        )
        if result.returncode != 0:
            raise OSError(result.stderr.strip()[-300:])
        return Path(result.stdout.strip())
    app = Path.home() / "Desktop" / "LMS Sync.app"
    command = (f"quoted form of {_applescript_string(sys.executable)} & \" \" & quoted form of "
               f"{_applescript_string(str(launcher))} & \" >/dev/null 2>&1 &\"")
    subprocess.run(["osacompile", "-o", str(app), "-e", f"do shell script {command}"], check=True, capture_output=True)
    return app


def schedule_daily(cli: Path, log: Path) -> None:
    """Run `cli.py daily` every morning; a run missed while the computer was off
    or asleep happens when it's back."""
    if WINDOWS:
        result = _powershell(
            "$ErrorActionPreference='Stop'; "
            "$a = New-ScheduledTaskAction -Execute $env:LMS_ARG0 -Argument ('\"' + $env:LMS_ARG1 + '\" daily') "
            "-WorkingDirectory $env:LMS_ARG2; "
            f"$t = New-ScheduledTaskTrigger -Daily -At {DAILY_HOUR}:00am; "
            "$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries; "
            f"Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $a -Trigger $t -Settings $s -Force | Out-Null",
            (background_python(), str(cli), str(cli.parent)),
        )
        if result.returncode != 0:
            raise OSError(result.stderr.strip()[-300:])
        return
    import plistlib

    log.parent.mkdir(parents=True, exist_ok=True)
    agent = Path.home() / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist"
    agent.parent.mkdir(parents=True, exist_ok=True)
    agent.write_bytes(plistlib.dumps({
        "Label": AGENT_LABEL,
        "ProgramArguments": [sys.executable, str(cli), "daily"],
        "StartCalendarInterval": {"Hour": DAILY_HOUR, "Minute": 0},
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }))
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{AGENT_LABEL}"], check=False, capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(agent)], check=True, capture_output=True)


def remove_integration() -> None:
    if WINDOWS:
        _powershell(
            f"Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false -ErrorAction SilentlyContinue; "
            "Remove-Item (Join-Path ([Environment]::GetFolderPath('Desktop')) 'LMS Sync.lnk') -ErrorAction SilentlyContinue")
        return
    import shutil

    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{AGENT_LABEL}"], check=False, capture_output=True)
    (Path.home() / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist").unlink(missing_ok=True)
    shutil.rmtree(Path.home() / "Desktop" / "LMS Sync.app", ignore_errors=True)
