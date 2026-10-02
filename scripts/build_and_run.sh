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
APP_RESOURCES="$APP_CONTENTS/Resources"
APP_BINARY="$APP_MACOS/$APP_EXECUTABLE"
SERVICE_BINARY="$APP_MACOS/okx-locald"
INFO_PLIST="$APP_CONTENTS/Info.plist"
ICONSET_DIR="$ROOT_DIR/Sources/MacTraderApp/Resources/NovaTrade.iconset"
ICON_FILE="$APP_RESOURCES/NovaTrade.icns"
DMG_FILE="$DIST_DIR/NovaTrade.dmg"

pkill -x "$APP_EXECUTABLE" >/dev/null 2>&1 || true
pkill -x "okx-locald" >/dev/null 2>&1 || true

cd "$ROOT_DIR"
swift build --product "$APP_EXECUTABLE"
swift build --product okx-locald

BUILD_BIN_DIR="$(swift build --show-bin-path)"
rm -rf "$APP_BUNDLE"
mkdir -p "$APP_MACOS" "$APP_RESOURCES"
cp "$BUILD_BIN_DIR/$APP_EXECUTABLE" "$APP_BINARY"
cp "$BUILD_BIN_DIR/okx-locald" "$SERVICE_BINARY"
chmod +x "$APP_BINARY"
chmod +x "$SERVICE_BINARY"
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
    lldb -- "$APP_BINARY"
    ;;
  --logs|logs)
    open_app
    /usr/bin/log stream --info --style compact --predicate "process == \"$APP_EXECUTABLE\""
    ;;
  --verify|verify)
    open_app
    sleep 1
    pgrep -x "$APP_EXECUTABLE" >/dev/null
    ;;
  *)
    echo "usage: $0 [run|package|--debug|--logs|--verify]" >&2
    exit 2
    ;;
esac
