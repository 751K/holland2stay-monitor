#!/bin/sh
# Xcode Cloud：把凭据注入截图用的 test plan。
#
# 为什么要绕这一道
# ----------------
# xcodebuild 只把带 TEST_RUNNER_ 前缀的环境变量转发给测试进程。而 Xcode Cloud
# **明确禁止**用这个前缀命名环境变量：
#
#   Variable name cannot start with "CI_" or "TEST_RUNNER_".
#
# 于是成了死结：带前缀的建不出来，不带前缀的到不了测试进程。
#
# 出路是绕开环境变量：test plan 自己支持 environmentVariableEntries，而 test
# plan 是仓库里的文件——构建前改它就行。本脚本在克隆之后、构建之前跑，把 Xcode
# Cloud 的（不带前缀的）环境变量写进去。
#
# 凭据不进仓库
# ------------
# 这个仓库是公开的。值只存在于 Xcode Cloud 的 Secret 环境变量里，构建时才注入
# 到工作副本，不会被提交。没设环境变量时脚本原样跳过——App 退回访客模式，只有
# Notifications 那一屏拍不到（访客的 tab bar 里没有 Alerts）。
set -eu

PLAN="$CI_PRIMARY_REPOSITORY_PATH/ios/FlatRadar/FlatRadar/Screenshots.xctestplan"

if [ ! -f "$PLAN" ]; then
    echo "找不到 test plan：$PLAN"
    exit 1
fi

if [ -z "${UI_TEST_USERNAME:-}" ] || [ -z "${UI_TEST_PASSWORD:-}" ]; then
    echo "未设置 UI_TEST_USERNAME / UI_TEST_PASSWORD，跳过注入（将以访客模式截图）"
    exit 0
fi

python3 - "$PLAN" <<'PY'
import json, os, sys

path = sys.argv[1]
with open(path, encoding="utf-8") as f:
    plan = json.load(f)

# 写进 defaultOptions，对所有 configuration（即所有语言）都生效。
opts = plan.setdefault("defaultOptions", {})
entries = [e for e in opts.get("environmentVariableEntries", [])
           if e.get("key") not in ("UI_TEST_USERNAME", "UI_TEST_PASSWORD")]
entries += [
    {"key": "UI_TEST_USERNAME", "value": os.environ["UI_TEST_USERNAME"]},
    {"key": "UI_TEST_PASSWORD", "value": os.environ["UI_TEST_PASSWORD"]},
]
opts["environmentVariableEntries"] = entries

with open(path, "w", encoding="utf-8") as f:
    json.dump(plan, f, ensure_ascii=False, indent=2)
    f.write("\n")

# 不打印值。只确认写进去了。
print("已注入 %d 个环境变量到 %s" % (len(entries), os.path.basename(path)))
PY
