"""
make_dark_icon.py — 从浅色图标反推出深色版本
=============================================
用法：
    pip install pillow numpy          # 仅此脚本需要，不是运行时依赖
    python tools/make_dark_icon.py            # 生成全部深色资源
    python tools/make_dark_icon.py --dry-run  # 只打印将要写入的文件

背景
----
图标没有矢量源文件，手上只有已经压平的 PNG：一层由白到浅蓝的背景渐变，
上面叠着半透明的房子图形，边缘还有一圈白色内辉光。直接反相或去饱和都不行——
反相会把渐变方向一起翻过来，去饱和只会得到一张灰白图（iOS 那张旧的
AppIcon-Dark.png 就是这么来的，在深色模式下反而更刺眼）。

所以这里先做分离，再重新合成：

1. 用「上包络」拟合估计背景。房子图形永远比背景暗，因此对亮度做迭代加权
   最小二乘、把暗于拟合面的点压到极低权重，收敛后得到的就是背景本身。
   基函数除了二维多项式，还带一组 exp(-dist/s)——dist 是到 squircle 边界的
   距离，用来吃掉那圈内辉光；没有这组基函数时，拟合会被角落的亮边带偏，
   图形密度图上会浮出一层雾。
2. density = 背景 - 原图，即每个像素上图形的"浓度"，抗锯齿和圆角都原样保留。
3. 把 density 当 alpha，合成到新的深色渐变上。

因此深色版和浅色版是同一个形状——不是重画的。原图换了的话重跑本脚本即可。
"""
from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 浅色母版。512×512 RGBA，alpha 就是 squircle 轮廓。
LIGHT_MASTER = ROOT / "static" / "logo.png"
# 同一张画的 1024 版本（已合成到白底），拿它当密度来源能多保住一倍细节。
LIGHT_MASTER_2X = (ROOT / "ios" / "FlatRadar" / "FlatRadar" / "Assets.xcassets"
                   / "AppIcon.appiconset" / "AppIcon.png")

# 深色配色。上深下亮，和浅色版"越往下越蓝"的走向保持一致。
BG_TOP, BG_BOTTOM = (17, 28, 46), (28, 78, 126)
MARK_TOP, MARK_BOTTOM = (205, 234, 255), (150, 205, 245)

# 网页资源：(浅色母版, 深色输出)。尺寸和 alpha 都取自左边那张——三张浅色图
# 当初不是一次导出的，各自的边缘抗锯齿略有出入，只有逐张沿用才能保证
# 深浅两版轮廓严格一致。
WEB_OUTPUTS = [
    (ROOT / "static" / "logo.png", ROOT / "static" / "logo-dark.png"),
    (ROOT / "static" / "logo-md.png", ROOT / "static" / "logo-md-dark.png"),
    (ROOT / "static" / "logo-small.png", ROOT / "static" / "logo-small-dark.png"),
]
# iOS 深色 App 图标：1024、不透明。角落也铺满深色——系统会再切一次圆角，
# 留白底的话会在边缘露出一圈亮边。
IOS_OUTPUT = (ROOT / "ios" / "FlatRadar" / "FlatRadar" / "Assets.xcassets"
              / "AppIcon.appiconset" / "AppIcon-Dark.png")


def _luma(rgb: np.ndarray) -> np.ndarray:
    return 0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]


def _distance_to_edge(mask: np.ndarray) -> np.ndarray:
    """到形状边界的距离（chamfer 3-4 近似，误差 ~2%，够用且不必引入 scipy）。

    画布边界算作外部，因此 squircle 在上下左右四条边的中点处距离为 0——
    那正是形状真正贴到画布边的地方。
    """
    d = np.pad(np.where(mask, 1e9, 0.0), 1, constant_values=0.0)
    xs = np.arange(d.shape[1], dtype=np.float64) * 3.0

    def scan_row(row: np.ndarray, prev: np.ndarray, forward: bool) -> np.ndarray:
        row = np.minimum(row, np.minimum(prev + 3.0, np.minimum(
            np.roll(prev, 1) + 4.0, np.roll(prev, -1) + 4.0)))
        # 行内传播 row[x] = min(row[x], row[x∓1] + 3) 是个前缀扫描：
        # min_j(row[j] + 3(x-j)) = 3x + 累积最小值(row - 3x)
        if forward:
            return np.minimum(row, xs + np.minimum.accumulate(row - xs))
        return np.minimum(row, np.minimum.accumulate((row + xs)[::-1])[::-1] - xs)

    for y in range(1, d.shape[0]):
        d[y] = scan_row(d[y], d[y - 1], True)
    for y in range(d.shape[0] - 2, -1, -1):
        d[y] = scan_row(d[y], d[y + 1], False)
    return d[1:-1, 1:-1] / 3.0


def _extract_density(luma: np.ndarray, mask: np.ndarray,
                     dist: np.ndarray) -> np.ndarray:
    """分离出图形浓度：0 = 纯背景，1 = 最实的笔画。"""
    h, w = luma.shape
    yy, xx = np.mgrid[0:h, 0:w]
    x = (xx / (w - 1)) * 2 - 1
    y = (yy / (h - 1)) * 2 - 1

    terms = [(x ** i) * (y ** j) for i in range(4) for j in range(4 - i)]
    for scale in (8.0, 20.0, 45.0, 100.0, 200.0):
        glow = np.exp(-dist / scale)
        terms.append(glow)          # 内辉光
        terms.append(glow * y)      # 辉光在上下两端的强度差

    fit_region = mask & (dist > 1)
    basis = np.stack([t[fit_region] for t in terms], 1)
    target = luma[fit_region]

    weight = np.ones_like(target)
    for _ in range(25):
        coef, *_ = np.linalg.lstsq(basis * weight[:, None], target * weight,
                                   rcond=None)
        resid = target - basis @ coef
        # 上包络：亮于拟合面的点（= 背景）留全权重，暗于拟合面的点（= 图形）压到 3%
        weight = np.where(resid >= 0, 1.0, 0.03)

    background = sum(c * t for c, t in zip(coef, terms))
    density = np.clip(background - luma, 0, None) * mask
    # 贴边 6px 内不要——那里是辉光的陡坡，拟合残差最大
    density *= np.clip((dist - 6) / 10, 0, 1)

    # 逐行去基线。源图的渐变本身带轻微横向条带，会在密度图上留下横纹；
    # 图形在任何一行都盖不满 70% 的列，25 分位数必然落在背景上，拿它当该行底噪。
    interior = np.where(dist > 20, density, np.nan)
    with warnings.catch_warnings():
        # 形状之外的行整行都是 NaN，nanpercentile 会为此告警——那些行本来就不参与
        warnings.simplefilter("ignore", RuntimeWarning)
        baseline = np.nan_to_num(np.nanpercentile(interior, 25, axis=1))[:, None]
    density = np.clip(density - baseline - 0.006, 0, None)

    peak = float(np.percentile(density[dist > 20], 99.9))
    return np.clip(density / peak, 0, 1)


def _vertical_ramp(top, bottom, t: np.ndarray) -> np.ndarray:
    c1 = np.array(top, float) / 255
    c2 = np.array(bottom, float) / 255
    return c1 + (c2 - c1) * t[..., None]


def _compose(density: np.ndarray) -> np.ndarray:
    """把图形浓度铺到深色渐变上，返回 [0,1] 的 RGB。"""
    h, w = density.shape
    yy, xx = np.mgrid[0:h, 0:w]
    v, u = yy / (h - 1), xx / (w - 1)

    background = _vertical_ramp(BG_TOP, BG_BOTTOM, np.clip(v * 0.82 + u * 0.18, 0, 1))
    mark = _vertical_ramp(MARK_TOP, MARK_BOTTOM, v)
    a = density[..., None]
    return np.clip(background * (1 - a) + mark * a, 0, 1)


def _write(path: Path, image: Image.Image, dry_run: bool) -> None:
    if dry_run:
        print(f"[dry-run] 将写入 {path.relative_to(ROOT)}  {image.size[0]}×{image.size[1]}")
        return
    image.save(path)
    print(f"已写入 {path.relative_to(ROOT)}  {image.size[0]}×{image.size[1]}  "
          f"{path.stat().st_size // 1024} KB")


def main() -> None:
    ap = argparse.ArgumentParser(description="从浅色图标生成深色版本")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要写入的文件")
    args = ap.parse_args()

    light = Image.open(LIGHT_MASTER).convert("RGBA")
    big = Image.open(LIGHT_MASTER_2X).convert("RGB")
    size = big.size[0]

    alpha = np.asarray(light.resize((size, size), Image.LANCZOS)
                       .getchannel("A")).astype(np.float64) / 255.0
    mask = alpha > 0.5
    dist = _distance_to_edge(mask)

    luma = _luma(np.asarray(big).astype(np.float64) / 255.0)
    density = _extract_density(luma, mask, dist)
    rgb = _compose(density)
    art = Image.fromarray((rgb * 255 + 0.5).astype(np.uint8), "RGB")

    # 网页：带 alpha 的 squircle，alpha 直接沿用同尺寸浅色图自己的通道
    for light_path, dark_path in WEB_OUTPUTS:
        counterpart = Image.open(light_path).convert("RGBA")
        out = art.resize(counterpart.size, Image.LANCZOS).convert("RGBA")
        out.putalpha(counterpart.getchannel("A"))
        _write(dark_path, out, args.dry_run)

    # iOS：不透明，角落也是深色
    _write(IOS_OUTPUT, art, args.dry_run)


if __name__ == "__main__":
    main()
