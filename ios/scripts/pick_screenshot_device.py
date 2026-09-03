#!/usr/bin/env python3
"""按 App Store 的截图尺寸挑一台模拟器。

与 ``pick_simulator.py`` 的区别
-------------------------------
那个脚本挑「任意一台最新的 iPhone」，跑单元测试够用。截图不行：App Store 的每
个 ``screenshotDisplayType`` 只接受特定的像素尺寸，挑错机型产出的图会被 ASC 拒
掉，而拒绝信息只说「尺寸不符」，不说该用哪台。

这里按显示类型登记候选机型，**从前往后**取第一台可用的。顺序即优先级：同一个
显示类型下，新机型排在前面，因为 runner 镜像更新时老机型先被删。

尺寸不在这里校验
----------------
本脚本只负责挑，真正的把关在流水线的「校验尺寸」一步——它读产出的 PNG，
逐张比对像素。理由是机型与像素的对应关系由 Apple 决定，我们抄一份下来就会过
时；而 PNG 的实际尺寸是事实，不会过时。

用法::

    xcrun simctl list devices available --json \\
      | python3 pick_screenshot_device.py APP_IPHONE_67

标准输出一行 ``<udid>\\t<name>\\tiOS <version>``；挑不出来退出码非 0。
"""
from __future__ import annotations

import json
import sys

_IOS_MARKER = "SimRuntime.iOS-"

#: 显示类型 → 可接受的机型名（按优先级从高到低）。
#:
#: 名字用 ``_name_matches`` 比对：整名相等，或后面紧跟一个括号后缀
#: （"iPad Pro 13-inch (M4) (2nd generation)"）。
#:
#: **不能用裸 startswith**：``"iPhone 17 Pro Max".startswith("iPhone 17 Pro")``
#: 为真，于是挑 6.3" 的 _61 会选到 Pro Max，产出 1320x2868 而不是 1206x2622。
DEVICE_CANDIDATES: dict[str, tuple[str, ...]] = {
    # 1206x2622。6.3" 的 Pro（非 Max），FlatRadar 目前实际在用的就是这一档
    # ——2026-09-03 的那批截图是从 iPhone 17 Pro 真机上传的。
    "APP_IPHONE_61": (
        "iPhone 17 Pro",
        "iPhone 16 Pro",
    ),
    # 1320x2868。Apple 仍把它归在 6.7" 这一档下。
    "APP_IPHONE_67": (
        "iPhone 17 Pro Max",
        "iPhone 16 Pro Max",
        "iPhone 15 Pro Max",
    ),
    # 2064x2752（竖）。13" M4 与 12.9" 共用同一个 displayType。
    "APP_IPAD_PRO_3GEN_129": (
        "iPad Pro 13-inch (M4)",
        "iPad Pro (12.9-inch)",
        "iPad Pro 11-inch (M4)",
    ),
}


def _name_matches(name: str, wanted: str) -> bool:
    """机型名是否就是 ``wanted``（允许一个括号后缀）。

    裸 ``startswith`` 会让 "iPhone 17 Pro" 咬中 "iPhone 17 Pro Max"——两者是
    不同的显示类型（6.3" vs 6.7"），尺寸差 114x246 像素，上传时才会被 ASC 拒。
    """
    return name == wanted or name.startswith(wanted + " (")


def _version(runtime: str) -> tuple[int, ...]:
    raw = runtime.split(_IOS_MARKER)[-1]
    try:
        return tuple(int(x) for x in raw.split("-"))
    except ValueError:
        return (0,)


def pick(payload: dict, display_type: str) -> tuple[str, str, str] | None:
    """返回 (udid, name, "iOS x.y")；挑不出来返回 None。

    候选顺序优先于系统版本：先满足机型，再在同机型里取最新的 iOS。反过来
    （先取最新 iOS）会在镜像同时装了新 iOS 的小屏机和旧 iOS 的 Pro Max 时
    挑错机型，而那正是尺寸不符的来源。
    """
    candidates = DEVICE_CANDIDATES.get(display_type)
    if not candidates:
        return None
    for wanted in candidates:
        best: tuple[tuple[int, ...], str, str, str] | None = None
        for runtime, devices in (payload.get("devices") or {}).items():
            if _IOS_MARKER not in runtime:
                continue
            ver = _version(runtime)
            for d in devices or []:
                if not d.get("isAvailable"):
                    continue
                name = d.get("name") or ""
                if not _name_matches(name, wanted):
                    continue
                cand = (ver, d["udid"], name, "iOS " + ".".join(str(x) for x in ver))
                if best is None or cand[0] > best[0]:
                    best = cand
        if best:
            return best[1], best[2], best[3]
    return None


def main() -> int:
    if len(sys.argv) != 2:
        print("用法: pick_screenshot_device.py <displayType>", file=sys.stderr)
        return 2
    display_type = sys.argv[1]
    if display_type not in DEVICE_CANDIDATES:
        print(f"未登记的 displayType: {display_type}；"
              f"已登记：{', '.join(sorted(DEVICE_CANDIDATES))}", file=sys.stderr)
        return 2
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(f"stdin 不是合法 JSON: {e}", file=sys.stderr)
        return 2
    got = pick(payload, display_type)
    if got is None:
        print(f"没有可用于 {display_type} 的模拟器；候选机型："
              f"{', '.join(DEVICE_CANDIDATES[display_type])}", file=sys.stderr)
        return 1
    print("\t".join(got))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
