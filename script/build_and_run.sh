#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-run}"
APP_NAME="NovaTrade"
APP_EXECUTABLE="mac-trader"
BUNDLE_ID="com.novatrade.desktop"
MIN_SYSTEM_VERSION="15.0"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="$ROOT_DIR/dist"
APP_BUNDLE="$DIST_DIR/$APP_NAME.app"
APP_CONTENTS="$APP_BUNDLE/Contents"
APP_MACOS="$APP_CONTENTS/MacOS"
APP_BINARY="$APP_MACOS/$APP_EXECUTABLE"
SERVICE_BINARY="$APP_MACOS/okx-locald"
INFO_PLIST="$APP_CONTENTS/Info.plist"

pkill -x "$APP_EXECUTABLE" >/dev/null 2>&1 || true
pkill -x "okx-locald" >/dev/null 2>&1 || true

cd "$ROOT_DIR"
# Also stop bundles launched directly from SwiftPM's build directory. Their
# process name can differ from the executable basename, leaving an old window
# visible after the fresh dist bundle is launched.
pkill -f "$ROOT_DIR/.build/NovaTrade.app/Contents/MacOS/$APP_EXECUTABLE" >/dev/null 2>&1 || true
swift build --product "$APP_EXECUTABLE"
swift build --product okx-locald

BUILD_BIN_DIR="$(swift build --show-bin-path)"
rm -rf "$APP_BUNDLE"
mkdir -p "$APP_MACOS"
cp "$BUILD_BIN_DIR/$APP_EXECUTABLE" "$APP_BINARY"
cp "$BUILD_BIN_DIR/okx-locald" "$SERVICE_BINARY"
chmod +x "$APP_BINARY"
chmod +x "$SERVICE_BINARY"

cat > "$INFO_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key>
  <string>$APP_EXECUTABLE</string>
  <key>CFBundleIdentifier</key>
  <string>$BUNDLE_ID</string>
  <key>CFBundleName</key>
  <string>$APP_NAME</string>
  <key>CFBundleDisplayName</key>
  <string>$APP_NAME</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>LSMinimumSystemVersion</key>
  <string>$MIN_SYSTEM_VERSION</string>
  <key>NSPrincipalClass</key>
  <string>NSApplication</string>
</dict>
</plist>
PLIST

open_app() {
  /usr/bin/open -n "$APP_BUNDLE"
}

case "$MODE" in
  run)
    open_app
    ;;
  --debug|debug)
    lldb -- "$APP_BINARY"
    ;;
  --logs|logs)
    open_app
    /usr/bin/log stream --info --style compact --predicate "process == \"$APP_EXECUTABLE\""
    ;;
  --telemetry|telemetry)
    open_app
    /usr/bin/log stream --info --style compact --predicate "subsystem == \"$BUNDLE_ID\""
    ;;
  --verify|verify)
    open_app
    sleep 1
    pgrep -x "$APP_EXECUTABLE" >/dev/null
    ;;
  *)
    echo "usage: $0 [run|--debug|--logs|--telemetry|--verify]" >&2
    exit 2
    ;;
esac
