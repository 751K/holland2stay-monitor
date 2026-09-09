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


# ── 网页资源与设计源同版 ────────────────────────────────────────────
#
# 2026-09-09：iOS 换成运河屋图标（FlatRadar-iOS 的 1839ba6）时，网页端的 8 个
# PNG 还是上一版的蓝色房子轮廓。那边同一次也漏了登录页的 BrandLogo——「换了图标，
# 但某一处还是上一版」在这个项目里已经是第三次。
#
# 漏得掉是因为这些 PNG 和设计源之间没有任何自动关系：尺寸对、格式对、深浅两版
# 齐全、alpha 轮廓一致，上面那 23 条断言全绿。它们检查的是资源**自洽**，而不是
# 资源**是不是这一版**。
#
# 这里补上后者。不重算像素（那要重现 LANCZOS 重采样，得引入 Pillow，而它不是本
# 项目的依赖），只比色：母版中心那栋红房子和四周底色，缩放不会改变它们。

MASTERS = ROOT / "tools" / "icon_masters"

#: 每个网页资源该对哪一张母版。
WEB_ASSETS = {
    "logo.png": "appicon-light-1024.png",
    "logo-md.png": "appicon-light-1024.png",
    "logo-small.png": "appicon-light-1024.png",
    "favicon.png": "appicon-light-1024.png",
    "apple-touch-icon.png": "appicon-light-1024.png",
    "logo-dark.png": "appicon-dark-1024.png",
    "logo-md-dark.png": "appicon-dark-1024.png",
    "logo-small-dark.png": "appicon-dark-1024.png",
}


def _pixel(decoded_img, fx: float, fy: float):
    w, h, px = decoded_img
    return px[int(h * fy) * w + int(w * fx)]


def _close(a, b, tol: int) -> bool:
    """缩放会让边缘像素混色，取样点选在平色区域，容差留给重采样的舍入。"""
    return all(abs(a[i] - b[i]) <= tol for i in range(3))


@pytest.mark.parametrize("asset,master", sorted(WEB_ASSETS.items()))
def test_web_asset_comes_from_the_current_master(asset, master):
    """网页上的图必须和 App 图标是同一版画。"""
    asset_path, master_path = STATIC / asset, MASTERS / master
    assert asset_path.exists(), f"缺少 {asset}"
    assert master_path.exists(), (
        f"缺少母版 {master}——它随仓库走，不是跨仓库引用；"
        "上一版脚本指向已删除的 ios/ 目录，一跑就 FileNotFoundError")

    a = _decode_png(asset_path)
    m = _decode_png(master_path)
    # 取样点要**在 squircle 里面、且离边够远**：靠近轮廓的像素带着 alpha 羽化，
    # 缩到 56 / 64 px 时会明显偏色。(0.08, 0.5) 在 64px 上落到 x=5，正好在羽化带
    # 上，第一版就是这么红的。下面两点在所有尺寸上都是平色区，实测最大偏差 1。
    for fx, fy, what in ((0.5, 0.5, "中心的房子"), (0.25, 0.15, "底色")):
        got, want = _pixel(a, fx, fy), _pixel(m, fx, fy)
        assert _close(got, want, 12), (
            f"{asset} 的{what}是 {got[:3]}，母版 {master} 是 {want[:3]}——"
            "网页资源还停在上一版图标，跑一次 tools/make_web_icons.py")


def test_the_generator_agrees_that_everything_is_current():
    """``tools/make_web_icons.py --check`` 必须是干净的。

    上面那条只比两个取样点；这条是像素级的兜底。需要 Pillow，没装就跳过——
    **跳过是有代价的**，所以两条都留着：取样比在任何环境下都跑。
    """
    import subprocess
    import sys

    pytest.importorskip("PIL", reason="生成器需要 Pillow，非本项目依赖")
    r = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "make_web_icons.py"), "--check"],
        capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, (r.stdout + r.stderr).strip()


def test_the_generators_comparison_looks_at_colour_not_just_alpha():
    """``_identical`` 必须比到 RGB。

    这是 2026-09-09 开发当天踩的坑：原先用
    ``ImageChops.difference(a, b).getbbox()``，而 Pillow 10 起 ``getbbox()`` 对
    RGBA 默认 ``alpha_only=True``——只看 alpha。深浅两版共用同一个蒙版，alpha 恒
    等，于是它对「整张画换掉了」返回 ``None``。表现是 ``logo.png`` 明明还是上一
    版的蓝房子，脚本报「已是最新」跳过，另外 6 个文件正常重写——**一半新一半旧**。

    ``--check`` 是资源新旧的第二道防线（第一道是上面的取样比对）。它自己坏掉时
    第一道仍然会红，但一道防线坏了而不自知，本身就是要修的东西。
    """
    pytest.importorskip("PIL", reason="生成器需要 Pillow，非本项目依赖")
    import importlib.util

    from PIL import Image

    spec = importlib.util.spec_from_file_location(
        "_mwi", ROOT / "tools" / "make_web_icons.py")
    mwi = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mwi)

    # 两张图：alpha 完全相同，RGB 完全不同——正是深浅两版共用蒙版的形状。
    mask = Image.new("L", (8, 8), 128)
    red, blue = Image.new("RGBA", (8, 8), (200, 30, 30, 255)), \
        Image.new("RGBA", (8, 8), (30, 30, 200, 255))
    red.putalpha(mask)
    blue.putalpha(mask)

    tmp = ROOT / "static" / "_tmp_cmp_probe.png"
    blue.save(tmp)
    try:
        assert not mwi._identical(red, tmp), (
            "alpha 相同、画面不同却判成「一致」——只比了 alpha")
        assert mwi._identical(blue, tmp), "同一张图却判成不一致"
    finally:
        tmp.unlink(missing_ok=True)
