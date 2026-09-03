#!/usr/bin/env python3
"""从 .xcresult 里提取 App Store 截图。

为什么不用 xcparse
------------------
原先流水线用的是 ``xcparse screenshots``。Xcode 26 的 result bundle 换了格式，
它读不了::

    Error: Unhandled test reference type Optional(XCParseCore.ActionTestPlanRunSummaries)

而且它是**静默**失败的：那一步在流水线里带着 ``|| true``，于是 Capture 步骤照样
是绿的，直到 Verify 数出 0 张 PNG 才暴露。

改用 Xcode 自带的 ``xcresulttool export attachments``，不引第三方依赖。

为什么还要这个脚本，而不是直接用那条命令
----------------------------------------
``export attachments`` 把附件全部导出成 UUID 文件名，并另写一份 manifest 描述
「哪个 UUID 对应哪个人类可读名」。它导出的**不只是截图**——还有每条用例的录屏
（.mp4）、失败快照（.jpeg）、日志（.txt）。直接把目录交给上传工具，会把录屏
一起传到 App Store 去。

本脚本按 ``ScreenshotTests.snap()`` 起的名字（``NN-页面名_设备_语言_…``）筛出
截图，按序号排序后重命名。序号即 App Store 里的展示顺序，所以排序不能只靠文件
系统的默认顺序。

用法::

    python3 extract_screenshots.py rb.xcresult out/ [--expect 7]
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

#: ``snap(named:)`` 生成的名字形如 ``03-Map_Clone-1-of-iPhone-17-Pro_en-US_0_<uuid>.png``。
#: 只认「两位数字 + 连字符 + 名字」开头的，录屏和失败快照都不长这样。
NAME_RE = re.compile(r"^(\d{2})-([A-Za-z0-9]+)_")


def exported(bundle: Path, workdir: Path) -> list[tuple[int, str, Path]]:
    """导出附件，返回 [(序号, 页面名, 文件路径)]，已按序号排序。"""
    subprocess.run(
        ["xcrun", "xcresulttool", "export", "attachments",
         "--path", str(bundle), "--output-path", str(workdir)],
        check=True, capture_output=True)

    manifest = workdir / "manifest.json"
    if not manifest.exists():
        raise SystemExit(f"没有生成 manifest：{manifest}")

    out: dict[int, tuple[int, str, Path]] = {}
    for entry in json.loads(manifest.read_text()):
        for att in entry.get("attachments") or []:
            human = att.get("suggestedHumanReadableName") or ""
            m = NAME_RE.match(human)
            if not m:
                continue          # 录屏 / 失败快照 / 日志
            src = workdir / att["exportedFileName"]
            if not src.exists() or src.suffix.lower() != ".png":
                continue
            idx = int(m.group(1))
            # 同一序号可能出现多次（-test-iterations 重试）。保留最后一次：
            # 重试是因为前一次失败，后一次才是成功那张。
            out[idx] = (idx, m.group(2), src)
    return [out[k] for k in sorted(out)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("xcresult")
    ap.add_argument("outdir")
    ap.add_argument("--expect", type=int, default=0,
                    help="期望张数；不符则退出码非 0")
    a = ap.parse_args()

    bundle = Path(a.xcresult)
    if not bundle.exists():
        print(f"result bundle 不存在: {bundle}", file=sys.stderr)
        return 1
    outdir = Path(a.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        shots = exported(bundle, Path(tmp))
        for idx, name, src in shots:
            dst = outdir / f"{idx:02d}-{name}.png"
            shutil.copy2(src, dst)
            print(f"  {dst.name}  ({src.stat().st_size // 1024} KB)")

    print(f"提取 {len(shots)} 张 → {outdir}")
    if a.expect and len(shots) != a.expect:
        print(f"期望 {a.expect} 张，实际 {len(shots)} 张", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
