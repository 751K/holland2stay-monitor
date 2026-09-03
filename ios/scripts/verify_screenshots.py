#!/usr/bin/env python3
"""校验导出的截图：数量、尺寸、以及每个页面都在。

为什么要单独一步
----------------
``xcodebuild test`` 跑完 UI 测试是绿的，不代表产出可用。它对以下三种情况一律
沉默：

1. **机型挑错**——图能出，尺寸不是 ASC 接受的那个。上传时才被拒，错误信息只
   说「尺寸不符」。
2. **某个页面没截到**——UI 测试里某条用例失败被 ``continueAfterFailure``
   吞掉，或者页面没加载出来截了张空白，数量就少了。
3. **语言没生效**——``UI_TEST_LOCALE`` 拼错时 App 会退回英文，图照出，
   五种语言得到五套一模一样的英文截图。这一条本脚本查不了（像素层面判断语言
   不现实），但数量和尺寸能查，见 ``--expect``。

不读 Apple 的尺寸表
-------------------
机型与像素的对应关系由 Apple 决定，抄一份下来会过时。这里只要求「同一批图尺寸
一致」，再把实际尺寸打印出来与 ``--width/--height`` 比对——后者由调用方从 ASC
现有截图里读出来，而不是我们猜。

用法::

    python3 verify_screenshots.py DIR --expect 7 --width 1320 --height 2868
"""
from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path


def png_size(path: Path) -> tuple[int, int]:
    """只读 PNG 头，不依赖 Pillow——CI 镜像里不一定有。"""
    with path.open("rb") as f:
        head = f.read(24)
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path.name} 不是 PNG")
    if head[12:16] != b"IHDR":
        raise ValueError(f"{path.name} 头部异常")
    return struct.unpack(">II", head[16:24])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("directory")
    ap.add_argument("--expect", type=int, required=True, help="期望的截图张数")
    ap.add_argument("--width", type=int, required=True)
    ap.add_argument("--height", type=int, required=True)
    a = ap.parse_args()

    d = Path(a.directory)
    if not d.is_dir():
        print(f"目录不存在: {d}", file=sys.stderr)
        return 1
    pngs = sorted(p for p in d.rglob("*.png"))

    print(f"目录 {d}: {len(pngs)} 张 PNG")
    problems: list[str] = []
    if len(pngs) != a.expect:
        problems.append(f"张数 {len(pngs)}，期望 {a.expect}")

    for p in pngs:
        try:
            w, h = png_size(p)
        except ValueError as e:
            problems.append(str(e))
            continue
        flag = "" if (w, h) == (a.width, a.height) else \
               f"  ← 期望 {a.width}x{a.height}"
        print(f"  {p.name:<44} {w}x{h}{flag}")
        if (w, h) != (a.width, a.height):
            problems.append(f"{p.name} 尺寸 {w}x{h}")

    if problems:
        print("\n不合格：", file=sys.stderr)
        for x in problems:
            print("  -", x, file=sys.stderr)
        return 1
    print("全部合格")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
