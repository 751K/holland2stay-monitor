#!/usr/bin/env bash
#
# FlatRadar 截图自动化封装。
#
# 用法
# ----
#   ./tools/screenshots/run.sh [locale] [device-name]
#
# 例子
# ----
#   ./tools/screenshots/run.sh en-US
#   ./tools/screenshots/run.sh zh-Hans "iPhone 17 Pro Max"
#   ./tools/screenshots/run.sh nl
#
# 默认 locale=en-US, device="iPhone 17 Pro Max"，OS 由 xcrun 找最新可用。
#
# 流程
# ----
# 1. 找/启动模拟器
# 2. status_bar override 9:41 + 满信号 + 满电（Apple 标准营销截图样式）
# 3. xcodebuild test 跑 ScreenshotTests
# 4. xcparse 提取 PNG
# 5. 清理文件名（去掉 Clone-X-of-... 前缀）
# 6. 输出到 ~/Desktop/flatradar-screenshots/<locale>/

set -euo pipefail

LOCALE="${1:-en-US}"
DEVICE="${2:-iPhone 17 Pro Max}"

PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
XCODEPROJ="$PROJECT_ROOT/ios/FlatRadar/FlatRadar.xcodeproj"
# 按 <locale>/<device> 分目录，避免多 device 截图同名互相覆盖
# 也跟 fastlane deliver 的目录约定一致，将来要切 fastlane 上传零迁移
DEVICE_SLUG=$(echo "$DEVICE" | tr ' ' '-')
OUT_DIR="$HOME/Desktop/flatradar-screenshots/$LOCALE/$DEVICE_SLUG"
RESULT_BUNDLE="/tmp/flatradar-shots-$LOCALE-$DEVICE_SLUG.xcresult"
DERIVED="/tmp/flatradar-dd-shots"

echo "═══ FlatRadar 截图运行 ═══"
echo "  locale = $LOCALE"
echo "  device = $DEVICE"
echo "  output = $OUT_DIR"
echo

# 1. 找模拟器 UDID（已 boot 优先）
UDID=$(xcrun simctl list devices available -j \
  | python3 -c "
import json, sys
data = json.load(sys.stdin)
target = '$DEVICE'
candidates = []
for runtime, devices in data['devices'].items():
    for d in devices:
        if d.get('name') == target and d.get('isAvailable'):
            candidates.append((d['udid'], d.get('state', ''), runtime))
# 优先已 Booted；其次按 OS 排序拿最新
candidates.sort(key=lambda x: (x[1] != 'Booted', x[2]), reverse=False)
if candidates:
    print(candidates[0][0])
")
if [ -z "$UDID" ]; then
  echo "❌ 未找到 device='$DEVICE'。可用 device:"
  xcrun simctl list devices available | grep -E "iPhone|iPad" | head -20
  exit 1
fi
echo "✓ 使用模拟器 UDID=$UDID"

# 2. 启动（如未启动）+ 强制 9:41 状态栏
state=$(xcrun simctl list devices -j | python3 -c "
import json, sys
data = json.load(sys.stdin)
for runtime, devices in data['devices'].items():
    for d in devices:
        if d['udid'] == '$UDID':
            print(d.get('state', ''))
            sys.exit(0)
")
if [ "$state" != "Booted" ]; then
  echo "› boot 模拟器…"
  xcrun simctl boot "$UDID" || true
  # 等 SpringBoard 起来
  xcrun simctl bootstatus "$UDID" -b
fi

# 状态栏覆盖：9:41 + 满信号 + 满电（Apple ASC 截图标准）
echo "› 设置 status bar (9:41, full bars, 100%)…"
xcrun simctl status_bar "$UDID" override \
  --time "9:41" \
  --dataNetwork wifi \
  --wifiMode active \
  --wifiBars 3 \
  --cellularMode active \
  --cellularBars 4 \
  --batteryState charged \
  --batteryLevel 100

# 3. 跑测试
rm -rf "$RESULT_BUNDLE"
echo "› xcodebuild test…"
xcodebuild test \
  -project "$XCODEPROJ" \
  -scheme FlatRadar \
  -destination "platform=iOS Simulator,id=$UDID" \
  -only-testing:FlatRadarUITests/ScreenshotTests \
  -resultBundlePath "$RESULT_BUNDLE" \
  -derivedDataPath "$DERIVED" \
  UI_TEST_LOCALE="$LOCALE" \
  2>&1 | grep -E "Test (Case|Suite)|passed|failed|error:" || true

# 4. xcparse 提取
which xcparse > /dev/null || {
  echo "❌ 需要 xcparse: brew tap chargepoint/xcparse && brew install xcparse"
  exit 1
}

mkdir -p "$OUT_DIR"
echo "› 提取截图到 $OUT_DIR…"
xcparse screenshots "$RESULT_BUNDLE" "$OUT_DIR" 2>&1 | tail -5

# 5. 清理文件名 (去掉 Clone-1-of-..._<DEVICE>_<LOCALE>_0_<UUID> 后缀)
echo "› 清理文件名…"
cd "$OUT_DIR"
for f in *.png; do
  # 提取开头 NN-Name 部分
  new=$(echo "$f" | sed -E 's/^([0-9]+-[A-Za-z]+)_.*$/\1.png/')
  if [ "$new" != "$f" ]; then
    mv -f "$f" "$new"
  fi
done
cd - > /dev/null

# 6. 重置 status bar（避免下次开模拟器仍是 9:41 影响开发体验）
# xcodebuild test 跑完会 shutdown 模拟器，clear 会报 405。先重新 boot 再 clear。
xcrun simctl boot "$UDID" 2>/dev/null || true
xcrun simctl bootstatus "$UDID" -b > /dev/null 2>&1 || true
xcrun simctl status_bar "$UDID" clear 2>/dev/null || true
xcrun simctl shutdown "$UDID" 2>/dev/null || true

echo
echo "✓ 完成。文件列表:"
ls -la "$OUT_DIR"
