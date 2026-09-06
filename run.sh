#!/usr/bin/env bash
# Bindery — create a venv, install deps, run src/main.py
# Usage:
#   ./run.sh "/path/to/dump" --dry-run
#   ./run.sh "/path/to/dump" --dest "/path/to/Audiobooks" --apply
#   ./run.sh "/path/to/ebooks" --media ebook --dest "/path/to/Books" --apply
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
if command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
elif command -v python >/dev/null 2>&1; then
  PYTHON=python
else
  echo "Python 3 is required. Install it from https://www.python.org/downloads/" >&2
  exit 1
fi
if ! "$PYTHON" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)"; then
  echo "Python 3.9 or newer is required." >&2
  "$PYTHON" --version >&2
  exit 1
fi
VENV="$ROOT/.venv"
if [ ! -d "$VENV" ]; then
  echo "Creating virtualenv…"
  "$PYTHON" -m venv "$VENV"
fi
if [ -x "$VENV/bin/python" ]; then
  VENV_PY="$VENV/bin/python"
elif [ -x "$VENV/Scripts/python.exe" ]; then
  VENV_PY="$VENV/Scripts/python.exe"
elif [ -x "$VENV/Scripts/python" ]; then
  VENV_PY="$VENV/Scripts/python"
else
  echo "Could not find the virtualenv Python at $VENV" >&2
  exit 1
fi
STAMP="$VENV/.bindery-installed"
REQ="$ROOT/requirements.txt"
if [ ! -f "$STAMP" ] || [ "$REQ" -nt "$STAMP" ]; then
  echo "Installing requirements…"
  "$VENV_PY" -m pip install --upgrade pip -q
  if [ -f "$REQ" ]; then
    "$VENV_PY" -m pip install -r "$REQ" -q
  fi
  touch "$STAMP"
fi
exec "$VENV_PY" "$ROOT/src/main.py" "$@"
