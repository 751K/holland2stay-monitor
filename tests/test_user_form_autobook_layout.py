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


#: 注册了 booker 的 source → 面板标题行里应当出现的徽标文字。
#:
#: 用它把 UI 和 ``BOOKER_REGISTRY`` 绑在一起：注册了 booker 却没给用户填凭据的
#: 入口，那个 booker 就是够不着的。少一条映射同样会失败——逼着加 booker 的人
#: 回来面对「用户怎么配它」这个问题。
PLATFORM_BADGES = {
    "holland2stay": "H2S",
    "xior": "Xior",
    "ourdomain": "OurDomain",
    "ourcampus": "OurCampus",
    "plaza": "Plaza",
}


class TestPlatformsAreVisuallySeparate:
    def test_every_registered_booker_has_a_badge_mapping(self):
        from bookers import BOOKER_REGISTRY
        assert set(PLATFORM_BADGES) == set(BOOKER_REGISTRY), (
            "注册了新 booker 就要在这里登记它的面板徽标——"
            "没有面板入口的 booker，用户没法配凭据，等于够不着")

    @pytest.mark.parametrize("badge", sorted(PLATFORM_BADGES.values()))
    def test_each_platform_panel_is_labelled(self, form_html, badge):
        """每块面板的标题行里要有平台名——这是「一眼看出这是哪家」的最低要求。"""
        heads = re.findall(r'<div class="ab-platform-head">(.*?)</div>', form_html, re.S)
        assert any(badge in h for h in heads), (
            f"没有哪块面板的标题行写着 {badge}；共 {len(heads)} 块")

    def test_no_empty_badges(self, form_html):
        """空徽标是纯噪音：一个灰底小方块，什么也没说。"""
        empties = re.findall(r'<span class="badge[^"]*">\s*</span>', form_html)
        assert not empties, f"标题行里有空徽标 {len(empties)} 个"

    def test_one_panel_per_booker_plus_one_shared(self, form_html):
        """面板数从 registry 推，不写死——写死的数字每加一个平台就要有人记得改。"""
        n = len(PLATFORM_BADGES)
        total = len(re.findall(r'<div class="ab-platform(?:\s|")', form_html))
        shared = len(re.findall(r'<div class="ab-platform ab-platform-shared"', form_html))
        assert (total, shared) == (n + 1, 1), (
            f"应当是 {n} 块平台 + 1 块共用，实际 total={total} shared={shared}")

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


class TestApplicantProfilePanel:
    """2026-09-15 反馈的截图：档案整块的输入框看不见、24 个字段挤成一行 6 列、
    背景调查三问各占一整行、缺项显示成 first_name 这种代码字段名。"""

    _TPL = Path(__file__).resolve().parent.parent / "templates" / "user_form.html"

    def _css(self) -> str:
        return re.sub(r"/\*.*?\*/", "", _CSS.read_text(encoding="utf-8"), flags=re.S)

    def test_shared_panel_is_not_see_through(self):
        """透明填充透出来的是页面底色 var(--bg)——正是 .form-input 的底色，框就隐形了。
        .ab-platform 那条注释早就写着这个坑，共用块用 transparent 又踩了一遍。"""
        m = re.search(r"\.ab-platform-shared\s*\{(.*?)\}", self._css(), re.S)
        assert m
        bg = re.search(r"(?<![\w-])background(?:-color)?\s*:\s*([^;]+)", m.group(1))
        assert not bg or not re.search(r"transparent|var\(--bg\)", bg.group(1)), (
            f"共用块填成了 {bg.group(1)}，档案输入框会隐形")

    def test_profile_grid_has_fixed_columns(self):
        """auto-fit 在宽屏上排出 6 列，标签和框对不齐、分组也看不出来。"""
        tpl = self._TPL.read_text(encoding="utf-8")
        m = re.search(r"\.prof-grid\s*\{(.*?)\}", tpl, re.S)
        assert m and "repeat(4" in m.group(1) and "auto-fit" not in m.group(1)

    def test_every_profile_class_used_is_defined(self):
        """背景调查三问曾挂在 class="profile-grid" 上，而 CSS 里只有 prof-grid——
        名字对不上，三问各占一整行，也没人报错。"""
        tpl = self._TPL.read_text(encoding="utf-8")
        used = set(re.findall(r'class="[^"]*?\b(prof(?:ile)?-[a-z0-9-]+)', tpl))
        defined = set(re.findall(r"\.(prof(?:ile)?-[a-z0-9-]+)\s*[{,.: ]", tpl + self._css()))
        assert used and not (used - defined), f"模板用了但没有样式的 class: {sorted(used - defined)}"

    def test_sections_render_in_form_order(self, form_html):
        order = [form_html.find(k) for k in (
            "Name &amp; contact", "Nationality &amp; ID", "Current address",
            "Study &amp; lease", "Screening")]
        if -1 in order:  # 中文界面
            order = [form_html.find(k) for k in (
                "姓名与联系方式", "国籍与证件", "当前住址", "学业与租期", "背景调查")]
        assert -1 not in order and order == sorted(order), order

    def test_every_missing_field_has_a_readable_label(self):
        """缺项必须用表单上的字段名说。模板里的映射漏一个，就会原样露出 date_of_birth。"""
        from config import ApplicantProfile
        tpl = self._TPL.read_text(encoding="utf-8")
        block = re.search(r"\{% set missing_labels = \{(.*?)\} %\}", tpl, re.S)
        assert block
        mapped = set(re.findall(r"'([a-z_]+)':\s*'profile_", block.group(1)))
        missing = set(ApplicantProfile().missing_fields())
        assert missing <= mapped, f"没有显示名的缺项: {sorted(missing - mapped)}"

    def test_incomplete_notice_shows_labels_not_field_names(self, admin_client):
        from config import AutoBookConfig, ApplicantProfile
        from users import UserConfig, load_users, save_users

        u = UserConfig(name="prof-incomplete")
        u.auto_book = AutoBookConfig(applicant_profile=ApplicantProfile(nationality="China"))
        save_users(load_users() + [u])
        uid = next(x.id for x in load_users() if x.name == "prof-incomplete")
        html = admin_client.get(f"/users/{uid}").get_data(as_text=True)

        m = re.search(r'<div class="prof-incomplete[^"]*">(.*?)</ul>', html, re.S)
        assert m, "档案不完整却没有提示"
        notice = m.group(1)
        assert "date_of_birth" not in notice and "first_name" not in notice
        assert ("Date of birth" in notice) or ("出生日期" in notice)

    def test_placeholders_are_marked_as_examples(self, form_html):
        """「China」「12」没有前缀时看上去像已经填好的值。"""
        for example in ("China", "5612 AB", "12"):
            bare = re.findall(rf'placeholder="{re.escape(example)}"', form_html)
            assert not bare, f"占位 {example!r} 没有「例：/ e.g.」前缀，会被看成已填的值"

    def test_doc_and_consent_are_rows_not_striped_cards(self, form_html):
        """2026-09-15 反馈：这两块不要用左侧色条的卡片样式，和上面几组保持同一种版式。"""
        assert "consent-box" not in form_html
        assert form_html.count('class="prof-row') >= 2

    def test_file_input_keeps_its_name_and_stays_submittable(self, form_html):
        """原生文件框藏进了按钮里——藏的方式不能是 display:none 之外还丢了 name，
        也不能挪出 <form>，否则保存时证件静默不上传。"""
        m = re.search(r'<label class="btn[^"]*prof-file-btn">(.*?)</label>', form_html, re.S)
        assert m and 'type="file"' in m.group(1) and 'name="AUTO_BOOK_ID_DOC"' in m.group(1)
        assert 'onchange="showPickedIdDoc(this)"' in m.group(1), "选了文件却看不到文件名"
        assert "function showPickedIdDoc" in form_html

    def test_consent_toggle_posts_the_same_field(self, form_html):
        m = re.search(r'<label class="toggle prof-toggle">(.*?)</label>', form_html, re.S)
        assert m and 'name="AUTO_BOOK_SCREENING_CONSENT"' in m.group(1) and 'value="true"' in m.group(1)
        assert "aria-labelledby" in m.group(1), "开关本身没有文字，得挂上说明的 id"


class TestAutoBookFilterComesFirst:
    """2026-09-15 反馈：先放过滤条件，再放账户；过滤块也要白底面板。"""

    def test_filter_panel_precedes_every_account_panel(self, form_html):
        f = form_html.find('<div class="ab-filter">')
        first_platform = form_html.find('<div class="ab-platform">')
        assert f != -1, "过滤块没有包进面板"
        assert f < first_platform, "过滤条件又排到账户后面去了"

    def test_filter_panel_holds_the_filter_fields(self, form_html):
        start = form_html.find('<div class="ab-filter">')
        end = form_html.find('<div class="ab-platform">', start)
        assert end != -1
        panel = form_html[start:end]
        for name in ("AUTO_BOOK_MAX_RENT", "AUTO_BOOK_ALLOWED_CITIES", "AUTO_BOOK_ALLOWED_ENERGY"):
            assert f'name="{name}"' in panel, f"{name} 不在过滤面板里"
        # 楼盘 / 片区的选项是 JS 按城市现拉的，新建页上没有带 name 的 input，认容器 id
        for dom_id in ("ab-building-dropdown", "ab-neighborhood-dropdown"):
            assert f'id="{dom_id}"' in panel, f"#{dom_id} 不在过滤面板里（JS 按 id 找它）"
        assert "copyNotifFilters()" in panel

    def test_filter_panel_shares_the_panel_fill(self):
        css = re.sub(r"/\*.*?\*/", "", _CSS.read_text(encoding="utf-8"), flags=re.S)
        m = re.search(r"\.ab-filter\s*,\s*\.ab-platform\s*\{(.*?)\}", css, re.S)
        assert m and "var(--surface)" in m.group(1)


def test_every_credential_field_the_parser_reads_exists_in_the_form(form_html):
    """解析器读的名字，模板里必须真有一个同名输入框。

    两边对不上是**完全静默**的：用户填了、保存了、页面不报错，库里却是空的。
    2026-09-17 加 OurCampus 面板时，把模板里的 name 改错一个字母，所有测试仍然
    全绿——因为持久化测试是直接 POST 字段名，绕过了渲染出来的表单。
    """
    src = (Path(__file__).resolve().parent.parent / "app" / "forms" / "user_form.py").read_text(
        encoding="utf-8")
    names = set(re.findall(r'["\'](AUTO_BOOK_[A-Z0-9_]*(?:EMAIL|PASSWORD|USERNAME))["\']', src))
    assert names, "没在 user_form.py 里找到任何凭据字段名，这条守卫失效了"
    missing = sorted(n for n in names if f'name="{n}"' not in form_html)
    assert not missing, f"解析器读这些字段，但表单里没有同名输入框: {missing}"
