#!/bin/bash
# Build Holland2Stay Monitor .dmg for macOS
# Prerequisites: macOS, Python 3.11+
set -e

APP_NAME="H2S Monitor"
DMG_NAME="Holland2Stay Monitor"

# Auto-detect Python: use conda env "daily" if available, otherwise plain python
if conda run -n daily python --version &>/dev/null 2>&1; then
    PYTHON="conda run -n daily python"
    PIP="conda run -n daily pip"
else
    PYTHON="python3"
    PIP="pip3"
fi
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="$PROJECT_DIR/build"
DIST_DIR="$PROJECT_DIR/dist"
APP_BUNDLE="$DIST_DIR/$APP_NAME.app"

echo "=== Step 1: Install PyInstaller ==="
$PIP install pyinstaller -q

echo "=== Step 2: Build binary with PyInstaller ==="
cd "$PROJECT_DIR"
$PYTHON -m PyInstaller --clean --distpath "$DIST_DIR" --workpath "$BUILD_DIR" h2s_monitor.spec

echo "=== Step 3: Create .app bundle ==="
rm -rf "$APP_BUNDLE"
mkdir -p "$APP_BUNDLE/Contents/MacOS"
mkdir -p "$APP_BUNDLE/Contents/Resources"

# Move binary into .app
cp "$DIST_DIR/h2s-monitor" "$APP_BUNDLE/Contents/MacOS/"

# Generate Info.plist
cat > "$APP_BUNDLE/Contents/Info.plist" << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>H2S Monitor</string>
    <key>CFBundleDisplayName</key>
    <string>H2S Monitor</string>
    <key>CFBundleIdentifier</key>
    <string>com.holland2stay.monitor</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundleExecutable</key>
    <string>h2s-monitor</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>LSBackgroundOnly</key>
    <false/>
    <key>LSUIElement</key>
    <false/>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>CFBundleIconFile</key>
    <string>icon.icns</string>
</dict>
</plist>
PLIST

# Generate icon.icns from asset/image.png
ICONSET_DIR="$BUILD_DIR/icon.iconset"
rm -rf "$ICONSET_DIR"
mkdir -p "$ICONSET_DIR"
PNG_SRC="$PROJECT_DIR/asset/image.png"

if [ -f "$PNG_SRC" ]; then
    echo "Generating icon.icns from asset/image.png..."
    sips -z 16 16     "$PNG_SRC" --out "$ICONSET_DIR/icon_16x16.png"
    sips -z 32 32     "$PNG_SRC" --out "$ICONSET_DIR/icon_16x16@2x.png"
    sips -z 32 32     "$PNG_SRC" --out "$ICONSET_DIR/icon_32x32.png"
    sips -z 64 64     "$PNG_SRC" --out "$ICONSET_DIR/icon_32x32@2x.png"
    sips -z 128 128   "$PNG_SRC" --out "$ICONSET_DIR/icon_128x128.png"
    sips -z 256 256   "$PNG_SRC" --out "$ICONSET_DIR/icon_128x128@2x.png"
    sips -z 256 256   "$PNG_SRC" --out "$ICONSET_DIR/icon_256x256.png"
    sips -z 512 512   "$PNG_SRC" --out "$ICONSET_DIR/icon_256x256@2x.png"
    sips -z 512 512   "$PNG_SRC" --out "$ICONSET_DIR/icon_512x512.png"
    sips -z 1024 1024 "$PNG_SRC" --out "$ICONSET_DIR/icon_512x512@2x.png"
    iconutil -c icns "$ICONSET_DIR" -o "$BUILD_DIR/icon.icns"
    cp "$BUILD_DIR/icon.icns" "$APP_BUNDLE/Contents/Resources/icon.icns"
    echo "Icon generated and added to .app bundle."
else
    echo "No asset/image.png found — skipping icon."
fi

echo "=== Step 4: Create .dmg ==="

# Eject any previously mounted DMG with the same volume name
if hdiutil info | grep -q "$DMG_NAME"; then
    echo "Ejecting existing mounted DMG..."
    hdiutil detach "/Volumes/$DMG_NAME" -force 2>/dev/null || true
    sleep 1
fi

rm -f "$DIST_DIR/$DMG_NAME.dmg"

# Create a temporary folder for DMG contents
DMG_SRC="$BUILD_DIR/dmg_src"
rm -rf "$DMG_SRC"
mkdir -p "$DMG_SRC"
cp -R "$APP_BUNDLE" "$DMG_SRC/"
# Create Applications symlink for drag-to-install
ln -s /Applications "$DMG_SRC/Applications"

hdiutil create -volname "$DMG_NAME" \
    -srcfolder "$DMG_SRC" \
    -ov -format UDZO \
    "$DIST_DIR/$DMG_NAME.dmg"

rm -rf "$DMG_SRC"

echo ""
echo "=== Done ==="
echo "DMG: $DIST_DIR/$DMG_NAME.dmg"
ls -lh "$DIST_DIR/$DMG_NAME.dmg"
