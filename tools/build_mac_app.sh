#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
APP_BUNDLE="$REPO_ROOT/ClipForge.app"
CONTENTS="$APP_BUNDLE/Contents"
MACOS_DIR="$CONTENTS/MacOS"
RESOURCES_DIR="$CONTENTS/Resources"
BUNDLED_APP_DIR="$RESOURCES_DIR/app"

echo "Building ClipForge.app in $REPO_ROOT..."

# 1. Clean previous build
rm -rf "$APP_BUNDLE"
mkdir -p "$MACOS_DIR" "$RESOURCES_DIR" "$BUNDLED_APP_DIR"

# 2. Generate AppIcon.icns if needed
ICNS_FILE="$SCRIPT_DIR/AppIcon.icns"
if [ ! -f "$ICNS_FILE" ]; then
  echo "Generating AppIcon.icns from extension/icons/128.png..."
  ICONSET_DIR="$(mktemp -d)/AppIcon.iconset"
  mkdir -p "$ICONSET_DIR"
  SRC_ICON="$REPO_ROOT/extension/icons/128.png"

  for s in 16 32 64 128 256 512; do
    sips -z "$s" "$s" "$SRC_ICON" --out "$ICONSET_DIR/icon_${s}x${s}.png" >/dev/null 2>&1 || true
    if [ $((s * 2)) -le 512 ]; then
      sips -z "$((s * 2))" "$((s * 2))" "$SRC_ICON" --out "$ICONSET_DIR/icon_${s}x${s}@2x.png" >/dev/null 2>&1 || true
    fi
  done
  iconutil -c icns "$ICONSET_DIR" -o "$ICNS_FILE"
  rm -rf "$(dirname "$ICONSET_DIR")"
fi
cp "$ICNS_FILE" "$RESOURCES_DIR/AppIcon.icns"

# 3. Create Info.plist
cat > "$CONTENTS/Info.plist" << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDevelopmentRegion</key>
    <string>en</string>
    <key>CFBundleExecutable</key>
    <string>ClipForge</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>CFBundleIdentifier</key>
    <string>com.clipforge.app</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>CFBundleName</key>
    <string>ClipForge</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>1.3.1</string>
    <key>CFBundleVersion</key>
    <string>1.3.1</string>
    <key>LSMinimumSystemVersion</key>
    <string>10.15</string>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
EOF

# 4. Create PkgInfo
printf "APPL????" > "$CONTENTS/PkgInfo"

# 5. Bundle application code
echo "Bundling application assets..."
cp -R "$REPO_ROOT/server" "$BUNDLED_APP_DIR/"
cp -R "$REPO_ROOT/extension" "$BUNDLED_APP_DIR/"
cp -R "$REPO_ROOT/tools" "$BUNDLED_APP_DIR/"
cp "$REPO_ROOT/requirements.txt" "$BUNDLED_APP_DIR/"
cp "$REPO_ROOT/pyproject.toml" "$BUNDLED_APP_DIR/"

# 6. Create the MacOS launcher executable
cat > "$MACOS_DIR/ClipForge" << 'LAUNCHER_EOF'
#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTENTS_DIR="$(cd "$DIR/.." && pwd)"
RESOURCES_DIR="$CONTENTS_DIR/Resources"
BUNDLE_APP="$RESOURCES_DIR/app"
OUTSIDE_REPO="$(cd "$CONTENTS_DIR/../.." && pwd)"

export PATH="/opt/homebrew/opt/node@22/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

# Determine app root and data locations
if [ -f "$OUTSIDE_REPO/server/app.py" ]; then
  APP_ROOT="$OUTSIDE_REPO"
  DATA_DIR="$APP_ROOT/data"
  LOCAL_VENV="$APP_ROOT/.venv"
else
  APP_ROOT="$BUNDLE_APP"
  SUPPORT_DIR="$HOME/Library/Application Support/ClipForge"
  mkdir -p "$SUPPORT_DIR/data"
  DATA_DIR="$SUPPORT_DIR/data"
  LOCAL_VENV="$SUPPORT_DIR/.venv"
fi

export CLIPFORGE_DATA="$DATA_DIR"
mkdir -p "$DATA_DIR"
LOG_FILE="$DATA_DIR/engine.log"

find_python() {
  if [ -x "$LOCAL_VENV/bin/python" ]; then
    echo "$LOCAL_VENV/bin/python"
    return 0
  fi
  for candidate in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
        echo "$candidate"
        return 0
      fi
    fi
  done
  for p in /opt/homebrew/bin/python3 /usr/local/bin/python3 /Library/Frameworks/Python.framework/Versions/*/bin/python3; do
    if [ -x "$p" ] && "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
      echo "$p"
      return 0
    fi
  done
  return 1
}

# Locate or initialize Python environment
if [ ! -x "$LOCAL_VENV/bin/python" ]; then
  SYS_PY="$(find_python || true)"
  if [ -z "$SYS_PY" ]; then
    osascript -e 'display alert "ClipForge requires Python 3.9+" message "Python 3.9 or newer was not found. Please install Python from python.org to use ClipForge." as critical'
    exit 1
  fi
  "$SYS_PY" -m venv "$LOCAL_VENV"
  "$LOCAL_VENV/bin/python" -m pip install -r "$APP_ROOT/requirements.txt" >> "$LOG_FILE" 2>&1
fi

PYTHON_BIN="$LOCAL_VENV/bin/python"
STUDIO_URL="http://127.0.0.1:43177/"
HEALTH_URL="http://127.0.0.1:43177/api/health"

open_window() {
  if [ -d "/Applications/Google Chrome.app" ]; then
    open -na "Google Chrome" --args --app="$STUDIO_URL"
  else
    open "$STUDIO_URL"
  fi
}

# Check if already running
if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
  open_window
  exit 0
fi

# Start the local engine in the background
cd "$APP_ROOT"
nohup "$PYTHON_BIN" -m uvicorn server.app:app --host 127.0.0.1 --port 43177 >> "$LOG_FILE" 2>&1 &

# Wait for server readiness
for _ in {1..100}; do
  if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
    open_window
    exit 0
  fi
  sleep 0.2
done

osascript -e 'display alert "ClipForge could not start" message "The local engine failed to become ready. Check '"$LOG_FILE"' for details." as critical'
exit 1
LAUNCHER_EOF

chmod +x "$MACOS_DIR/ClipForge"

echo "ClipForge.app built successfully at: $APP_BUNDLE"
