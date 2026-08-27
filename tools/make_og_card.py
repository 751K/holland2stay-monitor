"""make_og_card.py — 生成社交分享卡片 static/og-card.png
========================================================
用法::

    pip install pillow            # 仅此脚本需要，不是运行时依赖
    python tools/make_og_card.py
    python tools/make_og_card.py --dry-run

为什么需要这张图
----------------
链接被贴进 WhatsApp / Teams / Telegram / Discord / iMessage 时，对方客户端
去抓 ``og:image``。没有这张图，分享出去的就是一行光秃秃的网址。

2026-08-27 从 Caddy 日志实测：343 个独立访客里 220 个直接落在 ``/`` 且不带
referer——微信、WhatsApp、Teams 转发链接时全都剥掉 referer，所以这批人正是
被人传人带来的。卡片打的就是这条已经跑通的路。

尺寸与构图
----------
1200×630 是 og:image 的事实标准（1.91:1，Facebook/LinkedIn/Slack 按此裁切）。
关键内容全部压在左侧 2/3：Twitter 的 ``summary_large_image`` 不裁，但部分
IM 客户端会取中间的方形缩略图，右边缘放不住东西。

字体用 SF（``SFNS.ttf`` 的可变字重），站点用的是 Inter——两者同为几何无衬线，
在这个尺寸下差别看不出来，而 Inter 需要额外下载字体文件，为一张静态图引入
一份二进制资产不值得。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

BASE_DIR = Path(__file__).resolve().parent.parent
LOGO = BASE_DIR / "static" / "logo.png"
OUT = BASE_DIR / "static" / "og-card.png"

W, H = 1200, 630
PAD = 88

#: 取自 static/design.css 的浅色 token，保持和站点同一套颜色。
TEXT = (10, 10, 10)
TEXT2 = (107, 107, 115)
ACCENT = (15, 107, 122)

_SFNS = "/System/Library/Fonts/SFNS.ttf"


def _font(size: int, weight: str = "Regular") -> ImageFont.FreeTypeFont:
    """按字重取 SF。取不到就回落到 Arial——宁可字形不同，也别整个脚本跑不了。"""
    try:
        f = ImageFont.truetype(_SFNS, size)
        f.set_variation_by_name(weight)
        return f
    except Exception:
        name = "Arial Bold.ttf" if weight in ("Bold", "Heavy") else "Arial.ttf"
        return ImageFont.truetype(f"/System/Library/Fonts/Supplemental/{name}", size)


def _background() -> Image.Image:
    """白到浅蓝的竖向渐变，再叠三团低透明度的色斑。

    色斑对应 design.css 里的 ``--app-bg-gradient``（蓝/绿/琥珀三个 radial）。
    直接画实心圆再高斯模糊，比逐像素算径向渐变快两个数量级，而在 630px 高、
    半径 400px 的尺度上肉眼分不出来。
    """
    img = Image.new("RGB", (W, H), "#ffffff")
    top, bottom = (255, 255, 255), (232, 243, 251)
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / (H - 1)
        d.line([(0, y), (W, y)],
               fill=tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)))

    blob = Image.new("RGB", (W, H), (0, 0, 0))
    bd = ImageDraw.Draw(blob)
    for cx, cy, r, color in (
        (100, 240, 400, (2, 132, 199)),
        (1120, 520, 360, (14, 180, 120)),
    ):
        bd.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
    blob = blob.filter(ImageFilter.GaussianBlur(150))
    return Image.blend(img, Image.blend(img, blob, 0.14), 1.0)


def _alert_card(d: ImageDraw.ImageDraw, img: Image.Image) -> None:
    """右侧那张通知卡——一眼看懂产品是干什么的。

    卡片上是**真实抓到过的一条房源**——xior 的 Eindhoven Zernikestraat 1-222，
    价格/面积/租客资格都取自生产库。og 卡片会被各家平台长期缓存，编一个不存在
    的地址等于把假数据钉在此后每一次分享上。
    """
    x0, y0, x1, y1 = 700, 168, 1112, 462
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        [x0 + 6, y0 + 14, x1 + 6, y1 + 14], radius=24, fill=(15, 23, 42, 46))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(18)))

    d.rounded_rectangle([x0, y0, x1, y1], radius=24,
                        fill=(255, 255, 255), outline=(232, 232, 232), width=2)

    dot_y = y0 + 46
    d.ellipse([x0 + 36, dot_y - 8, x0 + 52, dot_y + 8], fill=(14, 180, 120))
    d.text((x0 + 68, dot_y - 15), "Available to book",
           font=_font(26, "Semibold"), fill=(14, 148, 100))
    d.text((x1 - 36, dot_y - 13), "now", font=_font(24, "Regular"),
           fill=(155, 155, 155), anchor="ra")

    d.text((x0 + 36, y0 + 92), "Zernikestraat 1-222",
           font=_font(34, "Semibold"), fill=TEXT)
    d.text((x0 + 36, y0 + 140), "Eindhoven", font=_font(26, "Regular"), fill=TEXT2)

    d.line([(x0 + 36, y0 + 190), (x1 - 36, y0 + 190)], fill=(240, 240, 240), width=2)

    for i, (label, value) in enumerate(
            (("Rent", "€781"), ("Area", "19 m²"), ("Tenant", "Student"))):
        cx = x0 + 36 + i * 118
        d.text((cx, y0 + 212), label, font=_font(20, "Regular"), fill=(155, 155, 155))
        d.text((cx, y0 + 240), value, font=_font(28, "Semibold"), fill=TEXT)


def build() -> Image.Image:
    img = _background().convert("RGBA")
    d = ImageDraw.Draw(img)

    logo = Image.open(LOGO).convert("RGBA").resize((132, 132), Image.LANCZOS)
    img.paste(logo, (PAD, 76), logo)

    d.text((PAD, 244), "FlatRadar", font=_font(96, "Bold"), fill=TEXT)
    # 36px 而不是更大：右侧通知卡从 x=700 起，42px 时这行会被压在卡片下面。
    d.text((PAD, 372), "Instant alerts for Dutch rental listings",
           font=_font(36, "Medium"), fill=TEXT2)
    d.text((PAD, 430), "Holland2Stay · Xior · OurDomain · OurCampus",
           font=_font(27, "Regular"), fill=TEXT2)

    domain = "flatradar.app"
    f = _font(34, "Semibold")
    d.text((PAD, H - PAD - 34), domain, font=f, fill=ACCENT)

    # 底部一条极细的分隔线，和站点 --border 同色，避免整张图在浅色聊天背景里
    # 糊成一片没有边界。
    d.line([(0, H - 4), (W, H - 4)], fill=(232, 232, 232), width=4)
    _alert_card(d, img)
    return img.convert("RGB")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not LOGO.exists():
        print(f"缺少 {LOGO}", file=sys.stderr)
        return 1
    img = build()
    if args.dry_run:
        print(f"将写入 {OUT}  {img.size}")
        return 0
    img.save(OUT, "PNG", optimize=True)
    print(f"已写入 {OUT}  {img.size}  {OUT.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
