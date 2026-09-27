#!/bin/zsh
set -eu

project_root="${0:A:h:h}"
cd "$project_root"
swift build --product mac-trader
swift build --product okx-locald
product_dir="$(swift build --show-bin-path)"
bundle_dir="$project_root/.build/NovaTrade.app"
mkdir -p "$bundle_dir/Contents/MacOS"
cp "$product_dir/mac-trader" "$bundle_dir/Contents/MacOS/mac-trader"
cp "$product_dir/okx-locald" "$bundle_dir/Contents/MacOS/okx-locald"
cat > "$bundle_dir/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>mac-trader</string>
<key>CFBundleIdentifier</key><string>com.novatrade.desktop</string>
<key>CFBundleName</key><string>NovaTrade</string>
<key>CFBundleDisplayName</key><string>NovaTrade</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleVersion</key><string>1</string>
<key>LSMinimumSystemVersion</key><string>15.0</string>
<key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST
open "$bundle_dir"
