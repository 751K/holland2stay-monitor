"""侧栏底部的对齐：通知角标（纵向）与「监控运行中」的圆点（横向）。

2026-08-21 反馈「没对齐」：角标写的是 ``top:4px``，而按钮高度不是固定值——
桌面端是 8px padding 加行高，移动端还叠了 ``min-height:44px``。浏览器实测：

    桌面（按钮 39.25px）   角标中心比按钮中心高 6.63px
    移动（按钮 44px）      高 9px

改成 ``top:50%`` + ``translateY(-50%)`` 之后两种情况都是 0。

用文本断言而不是渲染测试：这条规则的要害是「不许再写死 top」，而写死与否在
CSS 源文件里一眼可查，跑无头浏览器反而把一条一行的约束搞成了脆弱的集成测试。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_CSS = Path(__file__).resolve().parent.parent / "static" / "design.css"


def _css() -> str:
    """去掉注释再解析——注释里也会出现 ``top:`` 这样的字样。"""
    return re.sub(r"/\*.*?\*/", "", _CSS.read_text(encoding="utf-8"), flags=re.S)


def _block(name: str) -> str:
    m = re.search(re.escape(name) + r"\s*\{(.*?)\}", _css(), re.S)
    assert m, f"design.css 里找不到 {name}"
    return m.group(1)


class TestNotifBadgeIsCentered:
    def test_uses_percentage_top_not_a_fixed_offset(self):
        css = _block(".notif-badge")
        top = re.search(r"(?<![\w-])top\s*:\s*([^;]+);", css)
        assert top, ".notif-badge 没有 top"
        assert top.group(1).strip() == "50%", (
            "top 写死成了固定像素。按钮高度不固定（移动端还有 min-height:44px），"
            "写死必然在某一端偏移——实测桌面偏 6.63px、移动偏 9px"
        )

    def test_has_the_translate_that_makes_50_percent_mean_centered(self):
        """只有 top:50% 而没有 translateY(-50%)，角标会整体下移半个自身高度。"""
        css = _block(".notif-badge")
        assert "translateY(-50%)" in css.replace(" ", ""), (
            "top:50% 把角标的**上边**放在中线上，还要 translateY(-50%) 把它拉回来"
        )

    def test_the_button_height_really_is_not_fixed(self):
        """本条约束的前提：按钮没有固定高度。前提没了就该重新想，而不是照抄。"""
        trigger = _block(".notif-trigger")
        assert not re.search(r"(?<![\w-])height\s*:", trigger), (
            ".notif-trigger 现在有固定 height 了——若真固定下来，写死 top 也能对齐，"
            "但这条测试的理由就变了，请重新评估而不是直接删"
        )

    def test_mobile_override_still_changes_the_height(self):
        """移动端 min-height:44px 是偏移最大的那一档，它还在就说明约束仍必要。"""
        css = _css()
        assert re.search(r"\.notif-trigger\s*\{\s*min-height:\s*44px", css), (
            "移动端的 min-height 覆盖不见了"
        )


# ── 「监控运行中」的圆点要和下面几行的图标同一条中线 ────────────────


class TestMonitorDotSharesTheIconColumn:
    """2026-08-25 反馈「绿点的中心应该和下面图表对齐」。

    成因：下面每一行的图标都在一个 18px 宽、居中的盒子里（原先由模板里 7 份
    内联 style 各写一遍），而圆点只有 6px、没有盒子，中心因此左偏
    (18-6)/2 = 6px。

    改法是给圆点套一个同宽的盒子、圆点画在 ``::before`` 上，并把 18px 收进
    ``.sidebar-footer i`` 一处——这样两边的数字不会脱节。
    """

    def test_icon_column_width_is_defined_once_in_css(self):
        css = _css()
        m = re.search(r"\.sidebar-footer i\s*\{([^}]*)\}", css)
        assert m, "图标列宽没有收进 CSS，还散在模板的内联 style 里"
        assert re.search(r"(?<![\w-])width\s*:\s*18px", m.group(1))

    def test_template_has_no_inline_icon_width(self):
        base = (Path(__file__).resolve().parent.parent / "templates" / "base.html").read_text()
        assert "width:18px" not in base, (
            "模板里又出现内联的图标宽度——散成两处之后，圆点那边无从对齐")

    def test_dot_box_matches_the_icon_column(self):
        """两个宽度必须相等。这条是本类的要害，其余都是它的实现细节。"""
        css = _css()
        icon = re.search(r"\.sidebar-footer i\s*\{[^}]*?(?<![\w-])width\s*:\s*(\d+)px", css)
        dot = re.search(r"\.mon-dot\s*\{[^}]*?(?<![\w-])width\s*:\s*(\d+)px", css)
        assert icon and dot, "取不到宽度"
        assert icon.group(1) == dot.group(1), (
            f"图标列 {icon.group(1)}px、圆点盒 {dot.group(1)}px——中心必然错开 "
            f"{(int(icon.group(1)) - int(dot.group(1))) / 2:g}px")

    def test_dot_box_centres_its_content(self):
        decl = _block(".mon-dot")
        assert "justify-content:center" in decl.replace(" ", "")
        assert "align-items:center" in decl.replace(" ", "")

    def test_the_dot_itself_is_drawn_on_the_pseudo_element(self):
        """圆点画在 ::before 上，盒子才能是 18px 而圆点仍是 6px。"""
        css = _css()
        m = re.search(r"\.mon-dot::before\s*\{([^}]*)\}", css)
        assert m, "没有 .mon-dot::before —— 盒子撑到 18px 会变成一个大圆"
        assert re.search(r"(?<![\w-])width\s*:\s*6px", m.group(1))
        assert "border-radius:50%" in m.group(1).replace(" ", "")

    @pytest.mark.parametrize("state", ["running", "stopped"])
    def test_state_colours_target_the_pseudo_element(self, state):
        """颜色要落在 ::before 上；落在盒子上就是一个 18px 的色块。"""
        css = _css()
        assert re.search(r"\.mon-badge\." + state + r"\s+\.mon-dot::before\s*\{", css), (
            f"{state} 的颜色没落在 ::before 上")
