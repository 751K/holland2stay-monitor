#!/usr/bin/env python3
"""从 ``xcrun simctl list devices available --json`` 里挑一台模拟器。

为什么不在 workflow 里写死 "iPhone 16"
--------------------------------------
GitHub 的 macOS runner 镜像每隔几周就换一次，随之而来的是 Xcode 版本变、
预装的模拟器型号变。写死型号的话，某天镜像一更新，CI 报的是::

    xcodebuild: error: Unable to find a destination matching the provided
    destination specifier

——看上去像项目坏了，实际只是那台设备没了。这里改成挑「可用的最新 iOS
iPhone 模拟器」，型号换了不用改任何文件。

用法::

    xcrun simctl list devices available --json | python3 pick_simulator.py

标准输出是一行 ``<udid>\\t<name>\\tiOS <version>``；没有可用设备时退出码非 0。
"""
from __future__ import annotations

import json
import sys

_IOS_RUNTIME_MARKER = "SimRuntime.iOS-"


def pick(payload: dict) -> tuple[str, str, str] | None:
    """返回 (udid, name, version)；挑不出来返回 None。

    三道过滤，每一道都对应一种曾经踩过或可以预见的坑：

    - **只认 iOS runtime**：设备列表里还有 watchOS / tvOS / visionOS，
      名字里同样可能出现 "iPhone"（配对用的伴侣设备）。
    - **只认 isAvailable**：镜像里常留着没下载完整 runtime 的设备条目，
      选中它 xcodebuild 会在启动模拟器那一步才失败，错误信息很难读。
    - **按版本号元组比大小**：按字符串比的话 "iOS-9-0" 会大于 "iOS-18-4"。
    """
    best: tuple[tuple[int, ...], str, str, str] | None = None
    for runtime, devices in (payload.get("devices") or {}).items():
        if _IOS_RUNTIME_MARKER not in runtime:
            continue
        raw = runtime.split(_IOS_RUNTIME_MARKER)[-1]
        try:
            version = tuple(int(part) for part in raw.split("-"))
        except ValueError:
            continue
        for device in devices:
            if not device.get("isAvailable"):
                continue
            name = device.get("name", "")
            if not name.startswith("iPhone"):
                continue
            if best is None or version > best[0]:
                best = (version, device["udid"], name, raw.replace("-", "."))
    if best is None:
        return None
    return best[1], best[2], best[3]


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"simctl 输出不是合法 JSON: {exc}", file=sys.stderr)
        return 2
    picked = pick(payload)
    if picked is None:
        print("没有可用的 iPhone 模拟器——检查上一步 `xcrun simctl list runtimes` "
              "的输出，多半是 runner 镜像换了", file=sys.stderr)
        return 1
    udid, name, version = picked
    print(f"{udid}\t{name}\tiOS {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
