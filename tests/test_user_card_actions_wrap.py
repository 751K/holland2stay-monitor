"""用户卡片的操作按钮行必须能换行。

2026-08-25 反馈「有些宽度的时候会这样」，截图里「删除」被卡片右边缘裁掉一半。

原因是尺寸算不过来：卡片列是 ``minmax(360px,1fr)``，带拖拽手柄时左内边距 44px、
右 20px，内容区只剩 296px；而四个按钮（编辑 / 测试通知 / 停用 / 删除，每个都带
图标）连 gap 一共要 350px。``.user-card-actions`` 当时是 ``display:flex`` 且没有
``flex-wrap``，于是既不换行也不收缩，直接溢出卡片——而卡片没有 ``overflow``，
溢出的部分就压在卡片外面被背景盖住。

浏览器实测（1180px 视口，三列、卡片 369px）：

    修复前   scrollWidth 350 > clientWidth 305，一行，「删除」被裁
    修复后   不溢出，换成两行（编辑 / 测试通知 / 停用 + 删除）

用文本断言而不是无头渲染：这条约束的要害是「这一行必须允许换行」，写没写在 CSS
源文件里一眼可查；渲染测试反而把一行约束变成依赖字体度量的脆弱集成测试。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_CSS = Path(__file__).resolve().parent.parent / "static" / "design.css"
_USERS_HTML = Path(__file__).resolve().parent.parent / "templates" / "users.html"


def _css() -> str:
    """去掉注释再解析——注释里也写着 flex-wrap 这样的字样。"""
    return re.sub(r"/\*.*?\*/", "", _CSS.read_text(encoding="utf-8"), flags=re.S)


def _block(selector: str) -> str:
    m = re.search(re.escape(selector) + r"\s*\{(.*?)\}", _css(), re.S)
    assert m, f"design.css 里找不到 {selector}"
    return m.group(1)


class TestUserCardActionsWrap:
    def test_actions_row_may_wrap(self):
        decl = _block(".user-card-actions")
        wrap = re.search(r"(?<![\w-])flex-wrap\s*:\s*([^;]+)", decl)
        assert wrap, (
            ".user-card-actions 没有 flex-wrap——四个按钮在窄列里放不下，"
            "不换行就会溢出卡片，「删除」被右边缘裁掉")
        assert wrap.group(1).strip() == "wrap"

    def test_still_a_flex_row(self):
        """换行是加出来的，不是把布局换掉——gap 和 flex 都还要在。"""
        decl = _block(".user-card-actions")
        assert re.search(r"(?<![\w-])display\s*:\s*flex", decl)
        assert re.search(r"(?<![\w-])gap\s*:", decl), (
            "gap 同时管行距，换行之后没有它两行会贴在一起")

    def test_delete_stays_pushed_to_the_end(self):
        """``ml-auto`` 把删除推到行尾，和其余三个拉开距离。换行不该把它弄丢。"""
        html = _USERS_HTML.read_text(encoding="utf-8")
        m = re.search(r'<button[^>]*\bjs-delete-user\b[^>]*>', html, re.S)
        assert m, "users.html 里找不到删除按钮"
        cls = re.search(r'class="([^"]*)"', m.group(0))
        assert cls and "ml-auto" in cls.group(1).split(), (
            f"删除按钮的 class 里没有 ml-auto，操作行的视觉分组没了: {cls and cls.group(1)}")

    @pytest.mark.parametrize("selector", [".user-card-actions"])
    def test_no_fixed_width_on_the_row(self, selector):
        """写死宽度会把换行判断又变回算不准的尺寸游戏。"""
        decl = _block(selector)
        assert not re.search(r"(?<![\w-])width\s*:\s*\d", decl)
