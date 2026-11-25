#!/bin/bash
# LMS Sync installer for macOS.
#   curl -fsSL https://raw.githubusercontent.com/Pulkit-123/lms-sync-app/main/install.sh | bash
# Installs into ~/LMS-Sync (set LMS_SYNC_DIR to change), adds a Desktop app and a
# daily 8:00 sync (set LMS_SYNC_NO_DAILY=1 to skip). Safe to run again: your data stays.
set -euo pipefail

REPO="${LMS_SYNC_UPDATE_REPO:-Pulkit-123/lms-sync-app}"
API="${LMS_SYNC_UPDATE_API:-https://api.github.com}"
DIR="${LMS_SYNC_DIR:-$HOME/LMS-Sync}"

say() { printf '%s\n' "$*"; }
fail() { say "x $*" >&2; exit 1; }

[ "$(uname)" = "Darwin" ] || fail "This installer is for macOS. On Windows use install.ps1."
[ -e "$DIR/.git" ] && fail "$DIR is a development copy (a git checkout); not touching it."

PY=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3 /usr/bin/python3; do
  path="$(command -v "$candidate" 2>/dev/null)" || continue
  # /usr/bin/python3 is only a placeholder until Apple's command line tools are installed
  [ "$path" = "/usr/bin/python3" ] && ! xcode-select -p >/dev/null 2>&1 && continue
  if "$path" -c 'import sys, venv; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then PY="$path"; break; fi
done
[ -n "$PY" ] || fail "Python 3.9 or newer is needed. Run 'xcode-select --install' (or install from python.org), then run this again."
[ -d "/Applications/Google Chrome.app" ] || [ -d "$HOME/Applications/Google Chrome.app" ] || say "! Google Chrome wasn't found — LMS Sync needs it for signing in. Install it from google.com/chrome."

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
say "Downloading the latest release…"
ZIP_URL="$(curl -fsSL -H 'User-Agent: lms-sync-installer' "$API/repos/$REPO/releases/latest" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["zipball_url"])')" \
  || fail "Couldn't find a release to install."
curl -fsSL -H 'User-Agent: lms-sync-installer' -o "$TMP/app.zip" "$ZIP_URL"
mkdir "$TMP/x"
unzip -q "$TMP/app.zip" -d "$TMP/x"
TOP="$(find "$TMP/x" -mindepth 1 -maxdepth 1 -type d | head -1)"
[ -f "$TOP/src/cli.py" ] || fail "The download doesn't look like LMS Sync."
mkdir -p "$DIR"
cp -R "$TOP"/. "$DIR"/
chmod +x "$DIR/install.sh" "$DIR"/scripts/*.sh 2>/dev/null || true

say "Setting up Python (a minute or two the first time)…"
"$PY" -m venv "$DIR/.venv"
"$DIR/.venv/bin/python3" -m pip install -q --disable-pip-version-check -r "$DIR/requirements.txt"

if [ -n "${LMS_SYNC_NO_DAILY:-}" ]; then
  "$DIR/.venv/bin/python3" "$DIR/src/cli.py" install-integration --no-daily || say "! Some shortcuts couldn't be created."
else
  "$DIR/.venv/bin/python3" "$DIR/src/cli.py" install-integration || say "! Some shortcuts couldn't be created."
fi

say ""
say "Installed in $DIR. Open it any time with “LMS Sync” on your Desktop."
say "The page that opens walks you through adding the Chrome extension and logging in."
[ -n "${LMS_SYNC_NO_START:-}" ] || "$DIR/.venv/bin/python3" "$DIR/src/launcher.py"
