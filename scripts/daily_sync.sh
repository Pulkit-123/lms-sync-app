#!/bin/bash
# Entry point for the LaunchAgent. Never attempts interactive login — a
# stale session just gets reported via macOS notification (see cli.py's
# `daily` command). Logs go to logs/daily.log for troubleshooting; the
# notification is the primary signal, not the log.
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR/src"
{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S %z') ==="
  "$DIR/.venv/bin/python3" cli.py daily
} >> "$DIR/logs/daily.log" 2>&1
