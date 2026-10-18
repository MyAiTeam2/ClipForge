#!/bin/bash
set -e
cd "$(dirname "$0")"
export PATH="/opt/homebrew/opt/node@22/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
CLIPFORGE_PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    CLIPFORGE_PYTHON="$candidate"
    break
  fi
done
if [ -z "$CLIPFORGE_PYTHON" ]; then
  echo 'ClipForge needs Python 3.9 or newer. Install it from python.org, then open this file again.'
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
STUDIO_URL="http://127.0.0.1:43177/"
HEALTH_URL="http://127.0.0.1:43177/api/health"
if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
  echo 'ClipForge is already running. Opening Studio…'
  open "$STUDIO_URL"
  exit 0
fi
echo 'Starting ClipForge Studio…'
.venv/bin/python -m uvicorn server.app:app --host 127.0.0.1 --port 43177 &
ENGINE_PID=$!
trap 'kill "$ENGINE_PID" 2>/dev/null || true' EXIT INT TERM
for _ in {1..100}; do
  if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
    open "$STUDIO_URL"
    echo 'ClipForge Studio is ready. Keep this window open while you edit.'
    wait "$ENGINE_PID"
    exit 0
  fi
  sleep 0.2
done
echo 'ClipForge could not start. Check data/engine.log.'
exit 1
