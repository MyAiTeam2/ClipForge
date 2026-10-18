#!/bin/bash
set -e
cd "$(dirname "$0")"

CLIPFORGE_PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    CLIPFORGE_PYTHON="$candidate"
    break
  fi
done
if [ -z "$CLIPFORGE_PYTHON" ]; then
  echo 'Install Python 3.9 or newer from python.org, then run this installer again.'
  exit 1
fi
if [ -x .venv/bin/python ] && ! .venv/bin/python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  echo 'Replacing an older ClipForge Python environment…'
  rm -rf .venv
fi
if [ ! -x .venv/bin/python ]; then
  "$CLIPFORGE_PYTHON" -m venv .venv
fi
.venv/bin/python -m pip install -r requirements.txt

.venv/bin/python tools/install_companion.py

echo "ClipForge's automatic engine is installed."
echo "Load or reload the extension at chrome://extensions, then click ClipForge from any video page."
open -a "Google Chrome" "chrome://extensions"
