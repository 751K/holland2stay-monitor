"""render_bare_masters.py — 从设计源 SVG 出「无底色」母版
============================================================
用法（**只在 macOS 上能跑**，且只有设计源变了才需要跑）：

    python3 tools/render_bare_masters.py

产出 tools/icon_masters/bare-{light,dark}-1024.png：只有房子、窗户、运河线和倒影，
背景透明。登录页用它——那一页的图不该是一块贴在渐变上的方砖。

输入是 751K/FlatRadar-iOS 的 output/icon/appicon-{light,dark}.svg，路径由
``--svg-dir`` 指定，默认按同级目录找。**这是本仓库唯一一处跨仓库引用**，所以它
不参与日常构建：产物已经提交进 tools/icon_masters/，make_web_icons.py 只读产物。
上一版 make_dark_icon.py 就是因为把跨仓库路径写进日常路径，iOS 迁出之后一跑就
FileNotFoundError。

为什么要渲两次
--------------
macOS 没有随手可用的 SVG 栅格化器，``qlmanage -t`` 是最省事的一个——但它出的是
**缩略图**，一律铺白底，alpha 全是 255。所以把背景 rect 换成纯白渲一次、纯黑渲
一次，逐像素解出 alpha：

    白底  W = C + (1-a)·255        黑底  B = C
    ⇒ a = 1 - (W-B)/255，  颜色 = B/a

对正确的 alpha 合成这是精确解，不是近似——两次渲染的几何完全一致，差值只来自背景
透出来的那部分。
"""
from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys
import tempfile

import numpy as np
from PIL import Image

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "tools" / "icon_masters"
DEFAULT_SVG_DIR = ROOT.parent / "FlatRadar-iOS" / "output" / "icon"

#: 设计源里那个铺满的背景 rect。它是唯一一个没有 id 的顶层 rect。
BG_RECT = re.compile(r'<rect x="51\.7" y="35\.7"[^>]*?/>')


def _render(svg_text: str, size: int) -> np.ndarray:
    with tempfile.TemporaryDirectory() as d:
        src = pathlib.Path(d) / "a.svg"
        src.write_text(svg_text)
        subprocess.run(["qlmanage", "-t", "-s", str(size), "-o", d, str(src)],
                       capture_output=True, check=True)
        png = pathlib.Path(d) / "a.svg.png"
        if not png.exists():
            raise RuntimeError("qlmanage 没出图（非 macOS？）")
        with Image.open(png) as im:
            return np.asarray(im.convert("RGB")).astype(np.float64)


def bare(svg_path: pathlib.Path, size: int) -> Image.Image:
    s = svg_path.read_text()
    if not BG_RECT.search(s):
        raise SystemExit(f"{svg_path.name} 里没找到背景 rect——设计源的结构变了")

    def on(color: str) -> str:
        return BG_RECT.sub(
            f'<rect x="0" y="0" width="1024" height="1024" fill="{color}"/>',
            s, count=1)

    white, black = _render(on("#FFFFFF"), size), _render(on("#000000"), size)
    alpha = np.clip(1.0 - (white - black).mean(axis=2) / 255.0, 0.0, 1.0)
    rgb = np.zeros_like(black)
    solid = alpha > 1e-4
    for ch in range(3):
        rgb[..., ch][solid] = np.clip(black[..., ch][solid] / alpha[solid], 0, 255)
    return Image.fromarray(
        np.dstack([rgb, alpha * 255.0]).round().astype(np.uint8), "RGBA")


def main() -> int:
    ap = argparse.ArgumentParser(description="生成无底色的图标母版")
    ap.add_argument("--svg-dir", type=pathlib.Path, default=DEFAULT_SVG_DIR)
    ap.add_argument("--size", type=int, default=1024)
    args = ap.parse_args()

    if not args.svg_dir.is_dir():
        print(f"找不到设计源目录 {args.svg_dir}；用 --svg-dir 指过去",
              file=sys.stderr)
        return 2
    for which in ("light", "dark"):
        svg = args.svg_dir / f"appicon-{which}.svg"
        if not svg.exists():
            print(f"缺少 {svg}", file=sys.stderr)
            return 2
        img = bare(svg, args.size)
        target = OUT / f"bare-{which}-{args.size}.png"
        img.save(target)
        print(f"已写入 {target.relative_to(ROOT)}  {img.size[0]}×{img.size[1]}  "
              f"角 alpha={img.getpixel((2, 2))[3]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
