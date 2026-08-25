"""用户卡片的布局约束：卡片里的东西不许溢出卡片。

同一张卡片被同一类问题咬了两次，都是「flex 项默认不缩、内容又没有断行机会」：

1. **操作按钮行**（2026-08-25）。卡片列是 ``minmax(360px,1fr)``，带拖拽手柄时内容区
   只剩 296px，而四个按钮（编辑 / 测试通知 / 停用 / 删除）连 gap 要 350px。
   ``.user-card-actions`` 是 ``display:flex`` 且没有 ``flex-wrap``，既不换行也不收缩，
   「删除」被卡片右缘裁掉一半。

2. **头部的用户名**（2026-08-25）。用户名可能是一整个邮箱地址，没有空格断不开；
   左侧那组 flex 项默认 ``min-width:auto``，撑着不缩，于是右边的 ▲▼ 和状态胶囊被
   顶到卡片外面——实测 ``qijunhuang1221@gmail.com`` 把「启用」顶出右缘 83px。

用文本断言而不是无头渲染：这些约束的要害是「某条属性必须在」，写没写在源文件里
一眼可查；渲染测试反而把一行约束变成依赖字体度量的脆弱集成测试。
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


class TestLongUserNameCannotPushTheBadgeOut:
    """用户名是邮箱时，右边的 ▲▼ 和状态胶囊不许被顶出卡片。

    三件事缺一不可：左侧允许收缩、名字允许断行、右侧不许被压缩。少任何一条，
    ``qijunhuang1221@gmail.com`` 这种名字都会把「启用」推到卡片外面。
    """

    def test_identity_group_can_shrink(self):
        decl = _block(".user-card-identity")
        assert re.search(r"(?<![\w-])min-width\s*:\s*0", decl), (
            "左侧那组没有 min-width:0——flex 项默认 min-width:auto，"
            "长邮箱会撑住不缩，把右边的胶囊顶出卡片")

    def test_name_column_can_shrink_too(self):
        """名字/ID 那一列自己也是 flex 项，同样要能缩。"""
        decl = _block(".user-card-identity > div")
        assert re.search(r"(?<![\w-])min-width\s*:\s*0", decl)

    @pytest.mark.parametrize("selector", [".user-name", ".user-id"])
    def test_long_names_can_break_anywhere(self, selector):
        decl = _block(selector)
        assert re.search(r"(?<![\w-])overflow-wrap\s*:\s*(anywhere|break-word)", decl), (
            f"{selector} 没有 overflow-wrap——邮箱地址没有空格，断不开就只能溢出")

    def test_template_carries_the_hook(self):
        """CSS 挂在这个类上，模板不给就等于没修。"""
        html = _USERS_HTML.read_text(encoding="utf-8")
        assert "user-card-identity" in html, "users.html 的头部少了 user-card-identity"

    def test_no_inert_flex_shrink_on_the_controls(self):
        """实测右侧那组加 flex-shrink:0 毫无影响（胶囊和 ▲▼ 本来就缩不动）。

        留着会让人以为它在防什么。真要防，防的是 min-width，见上面两条。
        """
        css = _css()
        assert ".user-card-controls" not in css, (
            "又加回了实测不起作用的规则——逐条关掉的对照见 design.css 里的注释")
