"""自动预订那一段：三个平台各成一块，共用的申请人档案排在两个 RENTCafe 之后。

2026-08-25 反馈「三个平台之间区分不明显」。原样式是：H2S / Xior / OurDomain 各只有
一行灰色小标题（``text-xs font-semibold text-secondary``），字号比正文还小，三段之间
没有任何边界；更糟的是**申请人档案那一大块（20 多个字段 + 证件上传 + 同意书）夹在
Xior 和 OurDomain 之间**，读下来像是 Xior 一直没结束。

改成每个平台一块内嵌面板，并把申请人档案挪到两个 RENTCafe 平台之后——它归 Xior 和
OurDomain 共用（``bookers/rentcafe.py`` 一份代码填，XiorBooker / OurDomainBooker 都
继承它），夹在中间会让人以为它只属于 Xior。

顺序这条断言是本文件的重点：样式可以再调，顺序错了含义就是错的。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_CSS = Path(__file__).resolve().parent.parent / "static" / "design.css"


@pytest.fixture
def form_html(admin_client) -> str:
    r = admin_client.get("/users/new")
    assert r.status_code == 200
    return r.get_data(as_text=True)


class TestPlatformsAreVisuallySeparate:
    @pytest.mark.parametrize("badge", ["H2S", "Xior", "OurDomain"])
    def test_each_platform_panel_is_labelled(self, form_html, badge):
        """三块面板各自的标题行里要有平台名——这是「一眼看出这是哪家」的最低要求。"""
        heads = re.findall(r'<div class="ab-platform-head">(.*?)</div>', form_html, re.S)
        assert any(badge in h for h in heads), (
            f"没有哪块面板的标题行写着 {badge}；共 {len(heads)} 块")

    def test_no_empty_badges(self, form_html):
        """空徽标是纯噪音：一个灰底小方块，什么也没说。"""
        empties = re.findall(r'<span class="badge[^"]*">\s*</span>', form_html)
        assert not empties, f"标题行里有空徽标 {len(empties)} 个"

    def test_three_platform_panels_plus_one_shared(self, form_html):
        total = len(re.findall(r'<div class="ab-platform(?:\s|")', form_html))
        shared = len(re.findall(r'<div class="ab-platform ab-platform-shared"', form_html))
        assert (total, shared) == (4, 1), (
            f"应当是 3 块平台 + 1 块共用，实际 total={total} shared={shared}")

    def test_old_tiny_grey_headings_are_gone(self, form_html):
        """原来的小标题是 text-xs 灰字，和正文一样大——那正是「区分不明显」的成因。"""
        for key in ("Holland2Stay 账号", "Xior 账号", "OurDomain 账号",
                    "Holland2Stay account", "Xior account", "OurDomain account"):
            if key in form_html:
                idx = form_html.index(key)
                head = form_html.rfind('<div class="ab-platform-head">', 0, idx)
                assert head != -1 and idx - head < 400, (
                    f"「{key}」不在 ab-platform-head 里，又变回一行小灰字了")


class TestApplicantProfileSitsAfterBothRentCafePlatforms:
    """它是 Xior 和 OurDomain 共用的，不是 Xior 的一部分。"""

    def _pos(self, html: str) -> dict[str, int]:
        def find(*cands):
            for c in cands:
                i = html.find(c)
                if i != -1:
                    return i
            raise AssertionError(f"页面里找不到 {cands}")
        return {
            "xior": find("Xior 账号", "Xior account"),
            "ourdomain": find("OurDomain 账号", "OurDomain account"),
            "profile": find("申请人档案", "Applicant profile"),
        }

    def test_profile_comes_after_ourdomain(self, form_html):
        p = self._pos(form_html)
        assert p["xior"] < p["ourdomain"] < p["profile"], (
            "申请人档案又被夹回两个平台中间了——那会让 Xior 那块看起来没有结尾")

    def test_profile_block_says_who_shares_it(self, form_html):
        assert ("Xior 与 OurDomain 共用" in form_html
                or "Shared by Xior and OurDomain" in form_html), (
            "共用范围没写出来，用户无从判断这块资料是给谁填的")


class TestStyles:
    def _css(self) -> str:
        return re.sub(r"/\*.*?\*/", "", _CSS.read_text(encoding="utf-8"), flags=re.S)

    @pytest.mark.parametrize("selector", [".ab-platform", ".ab-platform-head",
                                          ".ab-platform-name", ".ab-platform-shared"])
    def test_selector_exists(self, selector):
        assert re.search(re.escape(selector) + r"\s*\{", self._css()), (
            f"design.css 里没有 {selector}")

    def test_panel_fill_is_not_the_input_colour(self):
        """面板不能用 var(--bg) 填充。

        ``.form-input`` 的底色正是 ``var(--bg)``，而且它既没有边框也没有阴影——
        面板一填同色，里面的输入框就整个隐形（第一版就是这么翻的，截图里
        「邮箱 / 密码」两个框直接看不见）。
        """
        m = re.search(r"\.ab-platform\s*\{(.*?)\}", self._css(), re.S)
        assert m
        bg = re.search(r"(?<![\w-])background\s*:\s*([^;]+)", m.group(1))
        assert bg and "var(--bg)" not in bg.group(1), (
            f"面板填成了 {bg.group(1) if bg else '?'}——和输入框同色，框会隐形")

    def test_head_row_wraps(self):
        """平台名 + 徽标一行放不下时要换行，别把「开发中」顶出面板。"""
        m = re.search(r"\.ab-platform-head\s*\{(.*?)\}", self._css(), re.S)
        assert m and re.search(r"(?<![\w-])flex-wrap\s*:\s*wrap", m.group(1))


class TestTranslationKeysAreUnique:
    """``user_form_profile`` 曾被定义两次，后一条把「申请人档案」覆盖成了「个人资料」。

    dict 字面量里重复的 key 不会报错，只会静默取最后一条——这类错误看代码看不出来，
    只能靠断言。
    """

    def test_no_duplicate_keys(self):
        src = (Path(__file__).resolve().parent.parent / "translations.py").read_text()
        keys = re.findall(r'^\s*"([a-z0-9_]+)":\s*\{', src, re.M)
        dupes = sorted({k for k in keys if keys.count(k) > 1})
        assert not dupes, f"translations.py 里有重复 key，后一条会静默覆盖前一条: {dupes}"
