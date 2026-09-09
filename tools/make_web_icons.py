"""make_web_icons.py — 从设计源生成网页端的全部图标资源
==========================================================
用法：
    pip install pillow            # 仅此脚本需要，不是运行时依赖
    python3 tools/make_web_icons.py            # 生成
    python3 tools/make_web_icons.py --check    # 只比对，不写（CI 用）

输入（都在 tools/icon_masters/，随仓库走）
------------------------------------------
    appicon-light-1024.png   浅色母版，满幅无 alpha
    appicon-dark-1024.png    深色母版，同上
    squircle-mask-512.png    网页用的 squircle 轮廓（alpha）

**母版是随仓库带的副本，不是跨仓库引用。** 上一版脚本指向
``ios/FlatRadar/.../AppIcon.png``，而 iOS 客户端 2026-09-05 迁出后那个路径就没
了——脚本从此一跑就 FileNotFoundError，而 23 条测试全绿，因为没有一条走真正的
入口。副本花 40 KB，换的是「这个仓库自己能把资源重新生成出来」。

同步来源：751K/FlatRadar-iOS 的 output/icon/AppIcon{,-Dark}.png，
由那边的 make-icons.py 从设计源 SVG 出。换图标时两边都要更新。

为什么网页要自己烤圆角
----------------------
iOS 的母版是**满幅、无 alpha、不烤圆角**的：平台自己上遮罩。网页没有这个平台，
而三个使用点里只有 ``.brand-mark`` 在 CSS 里写了 ``border-radius``，
``.login-logo`` 和 donate / legal / support 三页的 ``<img>`` 都靠 PNG 自带的
alpha 成形。所以这里把轮廓烤进 alpha——蒙版就是原来那批资源的 alpha 通道原样取
出来的，换的只是画面，形状一个像素都没变。

深浅两版共用同一个蒙版，因此轮廓逐像素相同；tests/test_dark_icon.py 里
``test_dark_keeps_the_same_silhouette`` 钉的就是这一条。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
MASTERS = ROOT / "tools" / "icon_masters"
STATIC = ROOT / "static"

LIGHT_MASTER = MASTERS / "appicon-light-1024.png"
DARK_MASTER = MASTERS / "appicon-dark-1024.png"
MASK = MASTERS / "squircle-mask-512.png"

#: 无底色母版：只有房子、窗户、运河线和倒影，背景透明。
#: 由 tools/render_bare_masters.py 从设计源 SVG 出（仅 macOS，设计变了才跑）。
BARE_LIGHT_MASTER = MASTERS / "bare-light-1024.png"
BARE_DARK_MASTER = MASTERS / "bare-dark-1024.png"

#: 目标文件 → (尺寸, 用深色母版吗)。
#:
#: favicon 与 apple-touch-icon 只有浅色一版：两者都由浏览器 / 系统按自己的规则
#: 呈现，没有主题分支可挂，给深色版也没有地方引用它。
OUTPUTS: dict[str, tuple[int, bool]] = {
    "logo.png":             (512, False),
    "logo-dark.png":        (512, True),
    "logo-md.png":          (112, False),
    "logo-md-dark.png":     (112, True),
    "logo-small.png":       (56,  False),
    "logo-small-dark.png":  (56,  True),
    "favicon.png":          (64,  False),
    "apple-touch-icon.png": (180, False),
}

#: 无底色的一版，登录页用。
#:
#: 那一页的图不该是一块贴在渐变上的方砖——它有自己的奶油底色，落在黄色渐变上就是
#: 一个边界分明的浅色方块。去掉底色之后房子直接坐在页面上。
#:
#: **不上 squircle 蒙版**：这一版本来就没有底，蒙版只会把倒影的两端切掉。
BARE_OUTPUTS: dict[str, tuple[int, bool]] = {
    "logo-bare.png":      (112, False),
    "logo-bare-dark.png": (112, True),
}


def _compose(dark: bool, size: int) -> Image.Image:
    """母版 → 上蒙版 → 缩到目标尺寸。

    **先在 512 上合成再缩**，不是先缩母版再缩蒙版分别合成：两条边分别重采样，
    边缘会错开半个像素，深浅两版的 alpha 就不再逐像素相同，
    ``test_dark_keeps_the_same_silhouette`` 会红——而它红得对，那种资源在深色
    主题下边缘会露出一圈浅色。
    """
    master = Image.open(DARK_MASTER if dark else LIGHT_MASTER).convert("RGB")
    mask = Image.open(MASK).convert("L")
    art = master.resize(mask.size, Image.LANCZOS)
    out = art.convert("RGBA")
    out.putalpha(mask)
    if size != mask.size[0]:
        out = out.resize((size, size), Image.LANCZOS)
    return out


def _bare(dark: bool, size: int) -> Image.Image:
    """无底色母版直接缩到目标尺寸——不上蒙版。

    它本来就没有底色，squircle 蒙版只会把左右两端的倒影切掉。
    """
    src = BARE_DARK_MASTER if dark else BARE_LIGHT_MASTER
    with Image.open(src) as im:
        return im.convert("RGBA").resize((size, size), Image.LANCZOS)


def _identical(a: Image.Image, path: Path) -> bool:
    """逐字节比。

    **不要用 ``ImageChops.difference(...).getbbox()``。** Pillow 10 起
    ``getbbox()`` 对 RGBA 默认 ``alpha_only=True``——只看 alpha 通道。而这里深浅
    两版共用同一个蒙版，alpha 恒等，于是它对「画面整个换掉了」返回 ``None``，
    判成「没差别」。开发时就是这样：``logo.png`` 明明还是上一版的蓝房子，脚本
    报「已是最新」跳过了它，另外 6 个文件却正常重写——一半新一半旧。
    """
    if not path.exists():
        return False
    b = Image.open(path).convert("RGBA")
    return a.size == b.size and a.tobytes() == b.tobytes()


def main() -> int:
    ap = argparse.ArgumentParser(description="生成网页端图标资源")
    ap.add_argument("--check", action="store_true",
                    help="只比对，不写；有差异时以 1 退出")
    args = ap.parse_args()

    for missing in (p for p in (LIGHT_MASTER, DARK_MASTER, MASK,
                                BARE_LIGHT_MASTER, BARE_DARK_MASTER)
                    if not p.exists()):
        print(f"缺少母版：{missing.relative_to(ROOT)}", file=sys.stderr)
        return 2

    stale: list[str] = []
    for name, (size, dark) in {**OUTPUTS, **BARE_OUTPUTS}.items():
        img = (_bare(dark, size) if name in BARE_OUTPUTS
               else _compose(dark, size))
        target = STATIC / name
        if _identical(img, target):
            continue
        stale.append(name)
        if not args.check:
            img.save(target)
            print(f"已写入 static/{name}  {size}×{size}  "
                  f"{target.stat().st_size // 1024} KB")

    if args.check:
        if stale:
            print("这些资源和母版对不上（跑一次 tools/make_web_icons.py）："
                  + "、".join(stale), file=sys.stderr)
            return 1
        print(f"{len(OUTPUTS) + len(BARE_OUTPUTS)} 个资源都和母版一致")
    elif not stale:
        print("全部已是最新，无需改动")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
