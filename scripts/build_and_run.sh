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
LEGACY_APP_BUNDLE="$ROOT_DIR/.build/$APP_NAME.app"
APP_CONTENTS="$APP_BUNDLE/Contents"
APP_MACOS="$APP_CONTENTS/MacOS"
APP_RESOURCES="$APP_CONTENTS/Resources"
APP_BINARY="$APP_MACOS/$APP_EXECUTABLE"
LEGACY_APP_BINARY="$LEGACY_APP_BUNDLE/Contents/MacOS/$APP_EXECUTABLE"
FASTAPI_DIR="$APP_MACOS/backend"
INFO_PLIST="$APP_CONTENTS/Info.plist"
ICONSET_DIR="$ROOT_DIR/Sources/MacTraderApp/Resources/NovaTrade.iconset"
ICON_FILE="$APP_RESOURCES/NovaTrade.icns"
DMG_FILE="$DIST_DIR/NovaTrade.dmg"

# Builds must not stop a running client or daemon: `package` may run while
# strategies are live, and another checkout can own a same-named process.
# Only the modes that launch the app or attach to it below stop anything, and
# each kill is matched on the full executable path so it cannot reach a
# different checkout's binaries.
stop_running() {
  pkill -f "^$APP_BINARY( |$)" >/dev/null 2>&1 || true
  pkill -f "^$LEGACY_APP_BINARY( |$)" >/dev/null 2>&1 || true
  # The bundled interpreter is not necessarily named python3 (the current
  # app uses the Xcode Python runtime), so match the copied backend path.
  pkill -f "${FASTAPI_DIR}/main.py" >/dev/null 2>&1 || true
  # Give SIGTERM a moment to release 8787 before the new app checks whether a
  # backend is already running; otherwise it can attach to an old bundle.
  for _ in {1..20}; do
    pgrep -f "${FASTAPI_DIR}/main.py" >/dev/null 2>&1 || break
    sleep 0.1
  done
}

# Older builds used .build/NovaTrade.app. Keeping that bundle beside dist/
# gives macOS two apps with the same bundle identifier, which makes appshot
# capture fail with Apple Event error -10018.
prepare_for_launch() {
  stop_running
  rm -rf "$LEGACY_APP_BUNDLE"
}

cd "$ROOT_DIR"
swift build --product "$APP_EXECUTABLE"

BUILD_BIN_DIR="$(swift build --show-bin-path)"
rm -rf "$APP_BUNDLE"
mkdir -p "$APP_MACOS" "$APP_RESOURCES"
cp "$BUILD_BIN_DIR/$APP_EXECUTABLE" "$APP_BINARY"
rm -rf "$FASTAPI_DIR"
cp -R "$ROOT_DIR/backend" "$FASTAPI_DIR"
chmod +x "$APP_BINARY"
iconutil -c icns "$ICONSET_DIR" -o "$ICON_FILE"

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
  <key>CFBundleIconFile</key>
  <string>NovaTrade</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleVersion</key>
  <string>1</string>
  <key>CFBundleShortVersionString</key>
  <string>1.0</string>
  <key>LSMinimumSystemVersion</key>
  <string>$MIN_SYSTEM_VERSION</string>
  <key>NSHighResolutionCapable</key>
  <true/>
  <key>NSPrincipalClass</key>
  <string>NSApplication</string>
</dict>
</plist>
PLIST

# SwiftPM signs the executable before the hand-built app bundle exists. Sign
# the completed bundle so its Info.plist, helper and icon resources validate
# together when it is copied or mounted from the installer.
codesign --force --deep --sign - "$APP_BUNDLE" >/dev/null

open_app() {
  /usr/bin/open -n "$APP_BUNDLE"
}

case "$MODE" in
  run)
    prepare_for_launch
    open_app
    ;;
  package|--package)
    staging_dir="$(mktemp -d "${TMPDIR:-/tmp}/NovaTrade-installer.XXXXXX")"
    trap 'rm -rf "$staging_dir"' EXIT
    ditto "$APP_BUNDLE" "$staging_dir/$APP_NAME.app"
    ln -s /Applications "$staging_dir/Applications"
    hdiutil create -volname "$APP_NAME" -srcfolder "$staging_dir" -ov -format UDZO "$DMG_FILE" >/dev/null
    printf 'Created %s\n' "$DMG_FILE"
    ;;
  --debug|debug)
    prepare_for_launch
    lldb -- "$APP_BINARY"
    ;;
  --logs|logs)
    prepare_for_launch
    open_app
    /usr/bin/log stream --info --style compact --predicate "process == \"$APP_EXECUTABLE\""
    ;;
  --verify|verify)
    prepare_for_launch
    open_app
    sleep 1
    pgrep -f "^$APP_BINARY( |$)" >/dev/null
    ;;
  *)
    echo "usage: $0 [run|package|--debug|--logs|--verify]" >&2
    exit 2
    ;;
esac
