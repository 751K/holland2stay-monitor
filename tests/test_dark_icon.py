"""
深色图标资源的守卫。

这些测试盯着两类回归：

1. 资源本身。深色版必须和浅色版是同一个轮廓（alpha 逐像素相同）、确实更暗、
   并且图形和底色之间还留着足够的对比——旧的 iOS AppIcon-Dark.png 就是一张
   去饱和的灰白图，在深色模式下比浅色版还刺眼，这种东西不能再混进来。
   （iOS 图标那两条已随客户端迁去 751K/FlatRadar-iOS 的
   tests/test_ios_dark_icon.py，这里只剩网页资源。）
2. 接线。模板里每出现一个 /static/logo* 引用，就必须有对应的主题分支，
   否则深色主题下会露出一块白底。

PNG 用 zlib + struct 手动解，不引入 Pillow：Pillow 不是本项目的运行时依赖，
用 importorskip 挡掉又等于这些断言在 CI 上根本不跑。
"""
from __future__ import annotations

import json
import re
import struct
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
APPICON = (ROOT / "ios" / "FlatRadar" / "FlatRadar" / "Assets.xcassets"
           / "AppIcon.appiconset")

# (浅色, 深色) 成对的网页资源
LOGO_PAIRS = [
    ("logo.png", "logo-dark.png"),
    ("logo-md.png", "logo-md-dark.png"),
    ("logo-small.png", "logo-small-dark.png"),
]


# ── 极简 PNG 解码（8bit，颜色类型 2 / 6）────────────────────────────
def _decode_png(path: Path) -> tuple[int, int, list[tuple[int, int, int, int]]]:
    """返回 (宽, 高, 像素列表)，像素为 (R, G, B, A)。"""
    raw = path.read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} 不是 PNG"

    pos, idat, width, height, ctype = 8, bytearray(), 0, 0, 0
    while pos < len(raw):
        (length,) = struct.unpack(">I", raw[pos:pos + 4])
        kind = raw[pos + 4:pos + 8]
        body = raw[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            width, height, depth, ctype = struct.unpack(">IIBB", body[:10])
            assert depth == 8, f"{path.name} 位深 {depth}，本解码器只支持 8"
            assert ctype in (2, 6), f"{path.name} 颜色类型 {ctype} 不支持"
            assert body[12] == 0, f"{path.name} 是隔行扫描，本解码器不支持"
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
        pos += 12 + length

    channels = 4 if ctype == 6 else 3
    data = zlib.decompress(bytes(idat))
    stride = width * channels
    out: list[tuple[int, int, int, int]] = []
    prev = bytearray(stride)
    at = 0
    for _ in range(height):
        filt = data[at]
        line = bytearray(data[at + 1:at + 1 + stride])
        at += 1 + stride
        for i in range(stride):
            a = line[i - channels] if i >= channels else 0
            b = prev[i]
            c = prev[i - channels] if i >= channels else 0
            if filt == 1:
                line[i] = (line[i] + a) & 0xFF
            elif filt == 2:
                line[i] = (line[i] + b) & 0xFF
            elif filt == 3:
                line[i] = (line[i] + (a + b) // 2) & 0xFF
            elif filt == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 0xFF
        for i in range(0, stride, channels):
            out.append((line[i], line[i + 1], line[i + 2],
                        line[i + 3] if channels == 4 else 255))
        prev = line
    return width, height, out


def _luma(px: tuple[int, int, int, int]) -> float:
    """WCAG 相对亮度。"""
    def lin(c: float) -> float:
        c /= 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin(px[0]) + 0.7152 * lin(px[1]) + 0.0722 * lin(px[2])


def _contrast(l1: float, l2: float) -> float:
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


@pytest.fixture(scope="module")
def decoded() -> dict[str, tuple[int, int, list]]:
    paths = [STATIC / n for pair in LOGO_PAIRS for n in pair]
    return {p.name: _decode_png(p) for p in paths if p.exists()}


# ── 资源本身 ─────────────────────────────────────────────────────
@pytest.mark.parametrize("light,dark", LOGO_PAIRS)
def test_dark_asset_exists(light, dark):
    assert (STATIC / light).exists(), f"浅色母版 {light} 不见了"
    assert (STATIC / dark).exists(), (
        f"{dark} 缺失——跑 python tools/make_dark_icon.py 生成")


@pytest.mark.parametrize("light,dark", LOGO_PAIRS)
def test_dark_matches_light_dimensions(light, dark, decoded):
    lw, lh, _ = decoded[light]
    dw, dh, _ = decoded[dark]
    assert (lw, lh) == (dw, dh), f"{dark} 尺寸和 {light} 对不上"


@pytest.mark.parametrize("light,dark", LOGO_PAIRS)
def test_dark_keeps_the_same_silhouette(light, dark, decoded):
    """alpha 必须逐像素一致：深色版是同一个形状换色，不是另画一个。"""
    _, _, lpx = decoded[light]
    _, _, dpx = decoded[dark]
    mismatch = sum(1 for a, b in zip(lpx, dpx) if a[3] != b[3])
    assert mismatch == 0, f"{dark} 的 alpha 与 {light} 有 {mismatch} 个像素不同"


@pytest.mark.parametrize("light,dark", LOGO_PAIRS)
def test_dark_is_actually_dark(light, dark, decoded):
    """整体亮度必须明显低于浅色版。去饱和得到的灰白图会在这里挂掉。"""
    def mean_luma(name: str) -> float:
        _, _, px = decoded[name]
        solid = [p for p in px if p[3] > 200]
        return sum(_luma(p) for p in solid) / len(solid)

    light_l, dark_l = mean_luma(light), mean_luma(dark)
    assert dark_l < 0.25, f"{dark} 平均亮度 {dark_l:.3f}，还是太亮"
    assert dark_l < light_l * 0.4, (
        f"{dark} 平均亮度 {dark_l:.3f} 相对 {light} 的 {light_l:.3f} 降得不够")


@pytest.mark.parametrize("_light,dark", LOGO_PAIRS)
def test_dark_mark_stays_legible(_light, dark, decoded):
    """房子图形和底色之间要留住对比，否则缩到 28px 就糊成一块。"""
    _, _, px = decoded[dark]
    solid = sorted((_luma(p) for p in px if p[3] > 200))
    background = solid[len(solid) // 4]          # 四分位数：稳稳落在底色上
    mark = solid[int(len(solid) * 0.99)]         # 最实的笔画
    ratio = _contrast(mark, background)
    assert ratio >= 2.0, f"{dark} 图形与底色对比只有 {ratio:.2f}:1"


# ── iOS ─────────────────────────────────────────────────────────
# ── 接线 ─────────────────────────────────────────────────────────
def test_sidebar_brand_mark_has_both_themes():
    css = (STATIC / "design.css").read_text()
    assert 'background:url("/static/logo-small.png")' in css
    assert ('[data-theme="dark"] .sidebar-brand .brand-mark{\n'
            '  background-image:url("/static/logo-small-dark.png");') in css


def test_login_logo_has_both_themes():
    css = (STATIC / "design.css").read_text()
    assert 'background:url("/static/logo-md.png")' in css
    assert '[data-theme="dark"] .login-logo{background-image:url("/static/logo-md-dark.png")}' in css


def test_preload_follows_the_resolved_theme():
    """LCP 用的 preload 必须跟着主题走，不能写死一版。"""
    head = (ROOT / "templates" / "base.html").read_text()
    assert '<link rel="preload" href="/static/logo-small.png"' not in head, (
        "静态 preload 会让一半用户预载错的那张图")
    assert "'/static/logo-small-dark.png'" in head
    assert "'/static/logo-small.png'" in head
    # 必须在设置 data-theme 之后，用的是同一个 t
    script = head[head.index("<script>"):head.index("</script>")]
    assert script.index("setAttribute('data-theme'") < script.index("pre.href")


@pytest.mark.parametrize("page", ["legal", "support", "donate"])
def test_standalone_pages_offer_a_dark_source(page):
    """这三个页面跟随系统深色模式，用 <picture media> 提供深色图。"""
    html = (ROOT / "templates" / f"{page}.html").read_text()
    assert '<source srcset="/static/logo-md-dark.png" media="(prefers-color-scheme: dark)">' in html
    assert '<img src="/static/logo-md.png"' in html


def test_no_template_hardcodes_a_light_only_logo():
    """新增 logo 引用时如果忘了深色分支，在这里挡下来。"""
    pattern = re.compile(r"/static/(logo[\w-]*\.png)")
    offenders = []
    for tpl in (ROOT / "templates").glob("*.html"):
        text = tpl.read_text()
        for name in set(pattern.findall(text)):
            if name.endswith("-dark.png"):
                continue
            dark = name.replace(".png", "-dark.png")
            if f"/static/{dark}" not in text:
                offenders.append(f"{tpl.name}: {name} 没有配套的 {dark}")
    assert not offenders, "以下 logo 引用只有浅色版：\n" + "\n".join(offenders)


def test_referenced_logo_files_all_exist():
    pattern = re.compile(r"/static/(logo[\w-]*\.png)")
    referenced = set()
    for path in list((ROOT / "templates").glob("*.html")) + [STATIC / "design.css"]:
        referenced.update(pattern.findall(path.read_text()))
    missing = [n for n in referenced if not (STATIC / n).exists()]
    assert not missing, f"模板/CSS 引用了不存在的文件：{missing}"
