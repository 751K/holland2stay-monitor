"""侧栏赞助入口。

/donate 页早就存在（支付宝 / 微信收款码 + GitHub Sponsors，完全公开无需登录），
但只有登录页链过去。登录之后的页面没有任何入口——真正在用的人反而找不到。

顺带把侧栏底部三个链接的内联样式收进 CSS：原先语言 / 隐私 / 条款各自写了一份
等价的 style + onmouseover + onmouseout，加第四个链接就是第四份复制。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_BASE = _ROOT / "templates" / "base.html"
_CSS = _ROOT / "static" / "design.css"


def _footer() -> str:
    s = _BASE.read_text(encoding="utf-8")
    i = s.index('class="sidebar-footer"')
    return s[i:s.index("</aside>", i)]


class TestDonateLinkIsInTheSidebar:
    def test_footer_links_to_the_donate_page(self):
        assert "url_for('donate_page')" in _footer()

    def test_it_is_not_gated_by_role(self):
        """未登录页面也要显示（登录页本来就有），所以不加 is_admin / is_user 判断。"""
        f = _footer()
        i = f.index("url_for('donate_page')")
        # 往前找最近的 {% if %} / {% endif %}，确认这一行不在任何角色分支里
        before = f[:i]
        assert before.count("{% if") == before.count("{% endif %}"), (
            "赞助入口被包进了某个 {% if %} 分支"
        )

    def test_opens_in_a_new_tab_safely(self):
        f = _footer()
        seg = f[f.index("url_for('donate_page')"):][:200]
        assert 'target="_blank"' in seg
        assert 'rel="noopener noreferrer"' in seg, "新标签页打开必须带 noopener"

    def test_uses_the_translation_key(self):
        assert "_('donate')" in _footer()

    def test_both_languages_are_defined(self):
        from translations import TRANSLATIONS
        assert set(TRANSLATIONS["donate"]) >= {"zh", "en"}
        assert all(TRANSLATIONS["donate"][k].strip() for k in ("zh", "en"))


class TestFooterStylingIsShared:
    """样式收进 CSS 之后，链接和按钮必须还是一模一样的外观。"""

    def test_css_covers_anchors_too(self):
        css = _CSS.read_text(encoding="utf-8")
        m = re.search(r"\.sidebar-footer button[^{]*\{", css)
        assert m and ".sidebar-footer a" in m.group(0), (
            "共享规则没有覆盖 <a>，链接会退回浏览器默认样式"
        )

    def test_hover_rule_covers_anchors(self):
        css = _CSS.read_text(encoding="utf-8")
        assert re.search(r"\.sidebar-footer a:hover", css), (
            "内联 onmouseover 已删除，:hover 必须由 CSS 接管"
        )

    def test_no_inline_style_blobs_left(self):
        """这条是本次重构的目的：不许再靠内联样式复制粘贴。"""
        f = _footer()
        for anchor in re.findall(r"<a\b[^>]*>", f):
            assert "onmouseover" not in anchor, f"内联 hover 又回来了：{anchor[:80]}"
            assert "transition:color" not in anchor, f"内联样式又回来了：{anchor[:80]}"

    def test_links_do_not_show_underline(self):
        css = _CSS.read_text(encoding="utf-8")
        m = re.search(r"\.sidebar-footer button[^{]*\{(.*?)\}", css, re.S)
        assert "text-decoration:none" in m.group(1).replace(" ", ""), (
            "去掉内联 text-decoration:none 后，共享规则必须补上，否则链接带下划线"
        )
