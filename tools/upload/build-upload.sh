#!/usr/bin/env bash
#
# FlatRadar archive + upload 流程。
#
# 前置条件
# --------
# - Xcode signing 配好（DEVELOPMENT_TEAM 已设，Sign in & Capabilities 自动签名）
# - ~/.config/asc/AuthKey_<KeyID>.p8 已就位
# - 已 symlink 到 ~/.appstoreconnect/private_keys/AuthKey_<KeyID>.p8
# - 已读 ~/.config/asc/config.json 拿 key_id / issuer_id
#
# 流程
# ----
# 1. xcodebuild archive (release, generic iOS)
# 2. xcodebuild -exportArchive 导出 .ipa
# 3. xcrun altool --upload-app 推到 App Store Connect
# 4. 10-30 分钟后 ASC 会处理完，build 出现在版本里
#
# 不做
# ----
# - 改版本号（要手动 bump MARKETING_VERSION/CURRENT_PROJECT_VERSION）
# - 提审（用 asc_api.py submit-metadata-only）

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
XCODEPROJ="$PROJECT_ROOT/ios/FlatRadar/FlatRadar.xcodeproj"
SCHEME="FlatRadar"
ARCHIVE_PATH="/tmp/FlatRadar.xcarchive"
EXPORT_PATH="/tmp/FlatRadar-export"

# 从 config.json 拿 key_id + issuer_id（避免命令行明文）
CONFIG="$HOME/.config/asc/config.json"
[ -f "$CONFIG" ] || { echo "❌ 缺 $CONFIG"; exit 1; }
KEY_ID=$(python3 -c "import json; print(json.load(open('$CONFIG'))['key_id'])")
ISSUER_ID=$(python3 -c "import json; print(json.load(open('$CONFIG'))['issuer_id'])")

# 读 build settings
MARKETING_VERSION=$(xcodebuild -project "$XCODEPROJ" -scheme "$SCHEME" -configuration Release -showBuildSettings 2>/dev/null \
  | awk -F' = ' '/MARKETING_VERSION =/ {print $2; exit}')
BUILD_NUM=$(xcodebuild -project "$XCODEPROJ" -scheme "$SCHEME" -configuration Release -showBuildSettings 2>/dev/null \
  | awk -F' = ' '/CURRENT_PROJECT_VERSION =/ {print $2; exit}')
TEAM=$(xcodebuild -project "$XCODEPROJ" -scheme "$SCHEME" -configuration Release -showBuildSettings 2>/dev/null \
  | awk -F' = ' '/DEVELOPMENT_TEAM =/ {print $2; exit}')

echo "═══ FlatRadar Archive + Upload ═══"
echo "  version = $MARKETING_VERSION ($BUILD_NUM)"
echo "  team    = $TEAM"
echo "  key id  = $KEY_ID"
echo

# Export options plist（每次重写，避免人工编辑丢失）
EXPORT_OPTS="/tmp/FlatRadar-exportOptions.plist"
cat > "$EXPORT_OPTS" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>method</key>
    <string>app-store-connect</string>
    <key>destination</key>
    <string>export</string>
    <key>uploadSymbols</key>
    <true/>
    <key>signingStyle</key>
    <string>automatic</string>
    <key>teamID</key>
    <string>$TEAM</string>
    <key>stripSwiftSymbols</key>
    <true/>
</dict>
</plist>
EOF

# 清空旧 archive / export
rm -rf "$ARCHIVE_PATH" "$EXPORT_PATH"

echo "═══ 1/3 xcodebuild archive (3-5 min) ═══"
xcodebuild archive \
  -project "$XCODEPROJ" \
  -scheme "$SCHEME" \
  -configuration Release \
  -archivePath "$ARCHIVE_PATH" \
  -destination 'generic/platform=iOS' \
  -allowProvisioningUpdates \
  | tail -20

echo
echo "═══ 2/3 xcodebuild -exportArchive ═══"
xcodebuild -exportArchive \
  -archivePath "$ARCHIVE_PATH" \
  -exportPath "$EXPORT_PATH" \
  -exportOptionsPlist "$EXPORT_OPTS" \
  -allowProvisioningUpdates \
  | tail -10

IPA=$(find "$EXPORT_PATH" -name "*.ipa" | head -1)
if [ -z "$IPA" ]; then
  echo "❌ 未找到 IPA"
  ls -la "$EXPORT_PATH"
  exit 1
fi
echo "  IPA: $IPA  ($(du -h "$IPA" | awk '{print $1}'))"

echo
echo "═══ 3/3 xcrun altool --upload-app ═══"
xcrun altool --upload-app \
  -f "$IPA" \
  -t ios \
  --apiKey "$KEY_ID" \
  --apiIssuer "$ISSUER_ID"

echo
echo "✓ 上传完成。10-30 分钟后 ASC 会处理完 binary。"
echo "  ASC: https://appstoreconnect.apple.com/apps/6769857080/distribution/ios"
echo "  之后跑 'python tools/asc/asc_api.py status' 看 build 状态"
