#!/bin/bash
# Opens the app (starting it in the background if needed). Used by older Desktop
# shortcuts; new installs call src/launcher.py directly.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec "$ROOT/.venv/bin/python3" "$ROOT/src/launcher.py"
