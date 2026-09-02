#!/usr/bin/env python3
"""从 .xcresult 里读出真正执行了多少条用例，一条都没跑就报错退出。

为什么需要这一步
----------------
``xcodebuild test`` 在**一条用例都没执行**时同样打印 "Test Succeeded"、同样退出
0。第一次跑这条流水线就撞上了：日志里从头到尾没有一个用例名，而 CI 是绿的——
分不清「全过了」和「压根没跑」。

这正是这个仓库反复出现的形状：把「不知道」当成一个确定的答案。绿色必须由
**执行条数**背书，不能由退出码背书。
"""
from __future__ import annotations

import json
import subprocess
import sys

MIN_TESTS = 1


def _run(args: list[str]) -> str | None:
    try:
        out = subprocess.run(args, capture_output=True, text=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return out.stdout


def summary(path: str) -> dict | None:
    """优先用 Xcode 16+ 的 test-results 子命令，失败退回 legacy 格式。"""
    raw = _run(["xcrun", "xcresulttool", "get", "test-results", "summary",
                "--path", path, "--format", "json"])
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
    raw = _run(["xcrun", "xcresulttool", "get", "--path", path,
                "--format", "json", "--legacy"])
    if not raw:
        return None
    try:
        blob = json.loads(raw)
    except json.JSONDecodeError:
        return None
    metrics = blob.get("metrics", {})
    return {
        "totalTestCount": metrics.get("testsCount", {}).get("_value"),
        "failedTests": metrics.get("testsFailedCount", {}).get("_value", 0),
        "_legacy": True,
    }


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: summarize_xcresult.py <path.xcresult>", file=sys.stderr)
        return 2
    data = summary(sys.argv[1])
    if data is None:
        print("读不出 .xcresult——无法确认测试是否真的执行过。"
              "宁可判失败：绿色必须有依据。", file=sys.stderr)
        return 3

    total = data.get("totalTestCount")
    passed = data.get("passedTests")
    failed = data.get("failedTests") or 0
    skipped = data.get("skippedTests") or 0
    if total is None and passed is not None:
        total = passed + failed + skipped

    print(f"执行 {total} 条：通过 {passed if passed is not None else '?'}，"
          f"失败 {failed}，跳过 {skipped}")
    for case in (data.get("testFailures") or []):
        print(f"  ✗ {case.get('testName')} — {case.get('failureText')}")

    if failed:
        print(f"有 {failed} 条失败", file=sys.stderr)
        return 1
    if not total or total < MIN_TESTS:
        print(f"只执行了 {total} 条用例。xcodebuild 在一条都没跑时同样报 "
              f"Test Succeeded——这种绿色没有意义，判失败。", file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
