# LMS Sync

An **unofficial** desktop helper for ISB students using Moodle (`elearn.isb.edu`).
It runs on your own computer (macOS or Windows) and opens in Google Chrome.

- **Files** — pick courses, sections or single files; new ones download into your folders every day
- **Announcements & deadlines** — with your submission status, assignment instructions and files, your own notes
- **Calendar** — every course's deadlines in one side panel
- **Chrome extension** — saves links, SharePoint files and locked PDFs using your own sign-in, and adds any open page to a course

Not affiliated with or endorsed by ISB. Use it with your own account only; course
material is for your personal use — don't redistribute what you download.

## Install

You need **Google Chrome** and **Python 3.9 or newer**.

**macOS** — open Terminal and run:

```bash
curl -fsSL https://raw.githubusercontent.com/Pulkit-123/lms-sync-app/main/install.sh | bash
```

If it asks for Python, run `xcode-select --install`, then run the line again.

**Windows** — open PowerShell and run:

```powershell
irm https://raw.githubusercontent.com/Pulkit-123/lms-sync-app/main/install.ps1 | iex
```

If it asks for Python, run `winget install Python.Python.3.12`, open a new PowerShell window and run the line again.

This installs into `LMS-Sync` in your home folder, adds an **LMS Sync** shortcut on
your Desktop and a daily sync at 8:00. (Read the script first if you like — it's short.
Set `LMS_SYNC_NO_DAILY=1` to skip the daily job.)

## First run

The page walks you through it:

1. In Chrome, open `chrome://extensions`, switch on **Developer mode**, click **Load unpacked** and choose the `extension` folder inside `LMS-Sync`.
2. Press **Log in to Moodle**. It happens in a tab of your own Chrome, so your saved password or passkey works; the app never sees your password.
3. In the left panel, **tick a course** to start syncing it (you'll choose its folder). Untick it when the course ends.

## Updates

New versions are installed automatically (when the app starts and before the daily
sync); you can switch that off in the menu under your name and update with one click
instead. Only published releases of this repository are installed, and your folders,
ticks, files and login are never touched. Because the app updates itself, you are
trusting this repository's maintainer — turn automatic updates off if you'd rather
review releases first.

## Your data

Everything stays on your computer, inside `LMS-Sync` (`data/` and `.env`). The `.env`
file holds your Moodle token — treat it like a password and never share that folder.
The app only listens on your own computer. It talks to ISB Moodle, to the links your
courses point at, and to GitHub for updates. **Log out** (menu under your name) deletes
the token.

## What it does and doesn't do

- Files you already have are never overwritten; a changed file arrives as `name (updated DATE)`.
- Moving or renaming downloads is fine — nothing is downloaded twice.
- Nothing is deleted unless you press **Delete file…** (it goes to the Trash / Recycle Bin).
- DRM-protected videos aren't downloaded. Some sites' "are you human" checks need you to click.
- Moodle hides some assignments after their date; the app remembers the ones it has seen, and you can add others yourself.

## Uninstall

```bash
cd ~/LMS-Sync && .venv/bin/python3 src/cli.py uninstall-integration
```

(Windows: `cd $HOME\LMS-Sync; .venv\Scripts\python.exe src\cli.py uninstall-integration`.)
Then delete the `LMS-Sync` folder and remove the extension in Chrome. Your course folders stay.

## For developers

Python standard library plus `requests`, `python-dotenv`, `playwright` and `Send2Trash`.
Run the app from a checkout with `python src/cli.py ui`; tests with
`python -m unittest discover tests`. A git checkout never updates itself.
