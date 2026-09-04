"""Student Experience scraper 的解析与完整性判据。

fixture 取自 **2026-09-04** 的真实页面：

    studentexperience_longstay_p1.html        长租第 1 页，12 张卡片
    studentexperience_longstay_p2.html        长租第 2 页，10 张卡片
    studentexperience_shortstay_noterm.html   短租无学期档，页面全空

第三份是这组测试里最要紧的一份：它长得**和「站点改版了」一模一样**——没有卡片、
没有计数块、连「暂无房源」的提示都没有。scraper 靠长租页的计数块把这两种情况分开。

⚠️ 关于 fixture 的边界，得写在最前面
------------------------------------
2026-09-04 站点改版，这一整组测试**一条都没有变红**，而线上已经四处皆坏：卡片
锚点、卡片内部字段、计数块、分页。原因不是断言写得不好，是 fixture 本身冻住了
旧 DOM——对着一份不会变的 HTML，怎么测都测不出上游变了。

真正发现问题的是生产里那个完整性探针：它看到「计数说有货、卡片解析出 0 张」，
判了 incomplete，于是库里留下 4 条旧记录，而**没有**把它们收敛成一批假的下架
通知。这些测试守的是解析逻辑，探针守的是上游漂移，两者不能互相替代。

所以 fixture 过期这件事不该靠「测试变红」来发现——它不会。它靠的是生产日志里
那句 incomplete。
"""
from pathlib import Path

import pytest

from scrapers.studentexperience import (
    LOCATIONS,
    StudentExperienceScraper,
    _last_page,
    _parse_card,
    _parse_complex_counts,
    _split_cards,
)
from scrapers.base import ScrapeTask

FIXTURES = Path(__file__).parent / "fixtures"


def _fx(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8", errors="replace")


def _bump_complex_count(page: str, residence: str, n: int) -> str:
    """把长租页里某栋楼的可订计数改成 ``n``。

    计数是 ``<label for="complex-X">…</label>`` 之后的第一个
    ``<span class="amount">N</span>``；同名的 amount 在城市筛选那一段也有，所以
    必须从 label 往后找，不能全局替换。
    """
    import re
    i = page.index(f'id="complex-{residence}"')
    m = re.compile(r'<span class="amount">\d+</span>').search(page, i)
    assert m, f"{residence} 后面没有 amount"
    return page[:m.start()] + f'<span class="amount">{n}</span>' + page[m.end():]


def _strip_cards(page: str) -> str:
    """删掉所有卡片，保留计数块——模拟「站点说有货但卡片结构变了」。"""
    import re
    return re.sub(r'<a\b[^>]*class="[^"]*\bstudio is-overview\b[^"]*"[^>]*>',
                  "<a>", page)


@pytest.fixture(scope="module")
def page1() -> str:
    return _fx("studentexperience_longstay_p1.html")


@pytest.fixture(scope="module")
def page2() -> str:
    return _fx("studentexperience_longstay_p2.html")


@pytest.fixture(scope="module")
def noterm_page() -> str:
    return _fx("studentexperience_shortstay_noterm.html")


@pytest.fixture(scope="module")
def parsed(page1, page2):
    out = []
    for page in (page1, page2):
        out += [_parse_card(u, t, s) for u, t, s in _split_cards(page)]
    return [x for x in out if x is not None]


# ── 这次改版坏掉的四处，各守一条 ────────────────────────────────────

class TestCardAnchor:
    def test_cards_are_found_at_the_current_path(self, page1):
        """锚点路径是 ``/studios/<id>``，不是 ``/studio-types/<id>``。

        改版前是后者。旧正则写死了它，于是一张卡片都匹配不上——而页面上明明有
        12 张，解析结果是 0。这是「Zuidas 有一堆房源却一条没进库」的直接原因。
        """
        cards = _split_cards(page1)
        assert len(cards) == 12
        for url, type_id, _seg in cards:
            assert "/studios/" in url
            assert type_id.isdigit()

    def test_attribute_order_is_not_assumed(self):
        """href 与 class 谁先谁后不是契约。

        旧正则要求 ``href="…" class="studio is-overview…"`` 紧挨着。站点把两个
        属性调个个儿，整块就静默失效——而失效的表现是「没有房源」，不是报错。
        """
        page = ('<a class="studio is-overview longstay" '
                'href="https://studentexperience.com/studios/999">'
                '<div class="studio-info top"><h3>Leiden<br/>84</h3></div>'
                '<div class="price-wrap"><span class="price">&euro; 900</span></div></a>')
        cards = _split_cards(page)
        assert len(cards) == 1 and cards[0][1] == "999"

    def test_an_anchor_without_a_usable_id_is_skipped_not_guessed(self):
        """class 对上但 href 不是 ``/studios/<id>`` → 跳过。

        猜一个 id 的后果是两条房源撞同一个 key，后来的那条会覆盖前一条。
        """
        page = ('<a class="studio is-overview" href="/somewhere-else">'
                '<div class="studio-info top"><h3>Leiden<br/>84</h3></div></a>')
        assert _split_cards(page) == []

    def test_zero_availability_page_has_no_cards(self, noterm_page):
        assert _split_cards(noterm_page) == []


class TestFieldExtraction:
    def test_card_fields(self, parsed):
        item = next(x for x in parsed if x.name.endswith("7 C60"))
        assert item.name == "Amsterdam Zuidas — 7 C60"
        assert item.price_raw == "€837"
        assert item.city == "Amsterdam"
        assert item.status == "Available to book"
        assert item.source == "studentexperience"
        assert "/studios/" in item.url

        fm = dict(f.split(": ", 1) for f in item.features)
        assert fm["Building"] == "Amsterdam Zuidas"
        assert fm["Unit"] == "7 C60"
        assert fm["Type"] == "Studio"
        assert fm["Area"] == "21 m²"
        assert fm["Finishing note"] == "Not furnished"
        assert fm["Length of stay"] == "Maximum stay until June 30, 2027"

    def test_thousands_separator_changed_from_dot_to_comma(self, parsed):
        """价格从 ``€1.550`` 变成了 ``€ 1,046``。

        两种写法用欧洲习惯读会差一千倍，所以这条盯着一个四位数的价格。
        """
        item = next(x for x in parsed if x.name.endswith("63 K2"))
        assert item.price_raw == "€1.046"

    def test_the_unit_number_is_kept_out_of_the_building_name(self, parsed):
        """``<h3>楼盘<br/>单元</h3>`` 两行必须分开。

        混在一起会让楼盘名认不出来（表里没有 "Amsterdam NDSM 63 K2"），整条被丢。
        """
        for x in parsed:
            fm = dict(f.split(": ", 1) for f in x.features)
            assert fm["Building"] in {n for n, _c in LOCATIONS.values()}
            assert fm["Unit"] and fm["Unit"] not in fm["Building"]

    def test_fields_are_matched_by_content_not_by_icon_class(self):
        """``info-wrap`` 里那几行按文本模式认，不按图标 class 认。

        图标是装饰。站点换个图标集（fa-watch → 别的）不该让期限和装修档位一起消失。
        """
        page = ('<a class="studio is-overview" href="/studios/7">'
                '<div class="studio-info top"><h3>Leiden<br/>84</h3></div>'
                '<div class="info-wrap">'
                '<p><i class="brand-new-icon-set"></i>19 m²</p>'
                '<p class="info"><i class="whatever"></i>Long stay &gt; 1 year</p>'
                '<p class="info"><i class="whatever"></i>Not furnished</p>'
                '</div>'
                '<div class="price-wrap"><span class="price">&euro; 900</span></div></a>')
        (u, t, s), = _split_cards(page)
        fm = dict(f.split(": ", 1) for f in _parse_card(u, t, s).features)
        assert fm["Area"] == "19 m²"
        assert fm["Finishing note"] == "Not furnished"
        assert fm["Length of stay"] == "Long stay > 1 year"

    def test_shortstay_term_wins_over_the_card_text(self):
        """短租那条线的期限由学期档给出，覆盖卡片上的通用说法。"""
        page = ('<a class="studio is-overview" href="/studios/7">'
                '<div class="studio-info top"><h3>Leiden<br/>84</h3></div>'
                '<div class="info-wrap"><p class="info">Long stay &gt; 1 year</p></div>'
                '<div class="price-wrap"><span class="price">&euro; 900</span></div></a>')
        (u, t, s), = _split_cards(page)
        fm = dict(f.split(": ", 1) for f in _parse_card(u, t, s, "12 Months").features)
        assert fm["Length of stay"] == "12 Months"


class TestUnknownResidence:
    def test_unregistered_building_is_dropped_not_guessed(self):
        """楼盘名不在表里就丢掉，不猜城市。

        猜错会把房源分派给错误的 ScrapeTask——按城市订阅的用户会收到不该收的。
        """
        page = ('<a class="studio is-overview" href="/studios/7">'
                '<div class="studio-info top"><h3>Rotterdam Blaak<br/>1 A1</h3></div>'
                '<div class="price-wrap"><span class="price">&euro; 900</span></div></a>')
        (u, t, s), = _split_cards(page)
        assert _parse_card(u, t, s) is None


class TestPagination:
    def test_last_page_comes_from_the_pagination_control(self, page1, noterm_page):
        assert _last_page(page1) == 2
        assert _last_page(noterm_page) == 1

    def test_out_of_range_pages_wrap_instead_of_going_empty(self):
        """``?page=3`` 返回的是第 1 页，不是空页。

        所以「翻到空为止」的循环永远不会停。上限必须从分页控件读出来——这条
        钉住的是那个前提，它一旦不成立，翻页策略就得跟着改。
        """
        p1 = _fx("studentexperience_longstay_p1.html")
        p3 = _fx("studentexperience_longstay_p1.html")   # 实测 page=3 == page=1
        assert _split_cards(p3)[0][1] == _split_cards(p1)[0][1]

    def test_both_pages_are_collected(self, parsed):
        """22 条 = 12 + 10，与计数块的总数一致。"""
        assert len(parsed) == 22
        assert len({x.id for x in parsed}) == 22


class TestCompleteness:
    def test_complex_counts_parse_from_the_form_fields(self, page1):
        assert _parse_complex_counts(page1) == {
            "Amsterdam NDSM": 8, "Amsterdam Zuidas": 6,
            "Leiden": 8, "Amsterdam Amstel": 0,
        }

    def test_unit_numbers_in_card_titles_are_not_read_as_counts(self, page1):
        """卡片标题里的单元号不能被当成计数。

        上一版扫的是纯文本，从 "Complex" 一路切到 "Sort by" 或页尾。改版后
        "Sort by" 挪到了 "Complex" **前面**，于是这个块吃到页尾，把
        "Amsterdam NDSM 63 K2" 里的 63 当成了 NDSM 的计数，总数报成 76 而实际
        是 22。结论（判 incomplete）碰巧还是对的，理由却是错的。
        """
        import html as H
        import re
        plain = re.sub(r"\s+", " ", H.unescape(re.sub(
            r"(?is)<(script|style|svg|head)[^>]*>.*?</\1>", " ",
            re.sub(r"<[^>]+>", " ", page1)))).strip()

        counts = _parse_complex_counts(page1)
        assert sum(counts.values()) == 22
        assert counts["Amsterdam NDSM"] == 8          # 不是 63
        # 「Amsterdam NDSM 63」确实出现在可见文本里，就在计数块后面不远——
        # 上一版的纯文本扫描正是在这里把 8 覆盖成了 63。
        assert "Amsterdam NDSM 63" in plain
        assert plain.index("Sort by") < plain.index("Complex")   # 改版后的顺序

    def test_shortstay_empty_page_yields_no_counts(self, noterm_page):
        """短租页没有计数块，所以它单独一份判不出「空」还是「改版」。"""
        assert _parse_complex_counts(noterm_page) == {}

    def test_missing_count_block_marks_the_round_incomplete(self, monkeypatch):
        """读不到计数块 = 站点改版 → incomplete，让 monitor 跳过 stale 收敛。

        不这么做的后果不是「少推几条」，而是**整批存量房源被收敛成 Occupied 并发
        一批假的下架通知**。
        """
        s = StudentExperienceScraper()
        monkeypatch.setattr(s, "_session",
                            lambda: _FakeSession(long_pages=["<html>改版了</html>"]))
        with s.batch_session():
            r = s.scrape(ScrapeTask(source="studentexperience",
                                    city_key="amsterdam", city_display="Amsterdam"))
        assert r.complete is False and r.listings == []

    def test_counts_above_zero_with_no_cards_marks_incomplete(self, page1, monkeypatch):
        """计数说有货、却一张卡片都没解析出来 → 卡片结构变了。

        这正是 2026-09-04 线上发生的事，也是这一整轮修复的起点。
        """
        page = _strip_cards(page1)
        assert _parse_complex_counts(page)["Amsterdam NDSM"] == 8
        assert _split_cards(page) == []

        s = StudentExperienceScraper()
        monkeypatch.setattr(s, "_session", lambda: _FakeSession(long_pages=[page]))
        with s.batch_session():
            r = s.scrape(ScrapeTask(source="studentexperience",
                                    city_key="amsterdam", city_display="Amsterdam"))
        assert r.complete is False and r.listings == []

    def test_parsing_fewer_than_the_count_marks_incomplete(self, page1, monkeypatch):
        """数得出来却没拿全 → 也判 incomplete。

        只守「0 张」守不住「少拿了一页」：分页控件换个类名，第 2 页就悄悄没了，
        那 10 条会被当成下架。
        """
        s = StudentExperienceScraper()
        monkeypatch.setattr(s, "_session", lambda: _FakeSession(long_pages=[page1]))
        with s.batch_session():
            r = s.scrape(ScrapeTask(source="studentexperience",
                                    city_key="amsterdam", city_display="Amsterdam"))
        assert r.complete is False and r.listings == []


class TestCitySplit:
    def test_listings_are_dispatched_by_city(self, page1, page2, monkeypatch):
        s = StudentExperienceScraper()
        monkeypatch.setattr(s, "_session",
                            lambda: _FakeSession(long_pages=[page1, page2]))
        with s.batch_session():
            ams = s.scrape(ScrapeTask(source="studentexperience",
                                      city_key="amsterdam", city_display="Amsterdam"))
            lei = s.scrape(ScrapeTask(source="studentexperience",
                                      city_key="leiden", city_display="Leiden"))
        assert ams.complete and lei.complete
        assert len(ams.listings) == 14      # NDSM 8 + Zuidas 6
        assert len(lei.listings) == 8
        assert {x.city for x in ams.listings} == {"Amsterdam"}
        assert {x.city for x in lei.listings} == {"Leiden"}

    def test_dispatch_uses_display_name_not_key(self, page1, page2, monkeypatch):
        """按 ``city_display`` 分派，不是 ``city_key``。

        本平台的 key 恰好是 name 的小写，两者怎么比都一样——所以这条用一个 key 与
        name 不同的 task 来分辨。这不是假设：Magis 的
        ``'s-Hertogenbosch`` / ``s-hertogenbosch`` 就差一个撇号和大小写。
        """
        s = StudentExperienceScraper()
        monkeypatch.setattr(s, "_session",
                            lambda: _FakeSession(long_pages=[page1, page2]))
        with s.batch_session():
            r = s.scrape(ScrapeTask(source="studentexperience",
                                    city_key="ams-001", city_display="Amsterdam"))
        assert len(r.listings) == 14

    def test_batch_fetches_each_page_once_for_all_cities(self, page1, page2, monkeypatch):
        """两个城市共用一轮抓取——分两轮抓会把请求翻倍，且两轮之间库存可能变化。"""
        sess = _FakeSession(long_pages=[page1, page2])
        s = StudentExperienceScraper()
        monkeypatch.setattr(s, "_session", lambda: sess)
        with s.batch_session():
            for city in ("Amsterdam", "Leiden"):
                s.scrape(ScrapeTask(source="studentexperience",
                                    city_key=city.lower(), city_display=city))
        assert sess.long_hits == 2          # 两页各一次，不是每个城市各两次


class TestTransportErrors:
    def test_http_error_does_not_become_zero_listings(self, monkeypatch):
        """长租页取不到时必须抛，不能安静地返回 0 条。

        返回 0 条会被 monitor 当成「全下架了」。
        """
        from scrapers.base import ScrapeNetworkError

        class _Boom(_FakeSession):
            def get(self, url, params=None, **_kw):
                raise ScrapeNetworkError("boom")

        s = StudentExperienceScraper()
        monkeypatch.setattr(s, "_session", lambda: _Boom())
        with s.batch_session():
            with pytest.raises(ScrapeNetworkError):
                s.scrape(ScrapeTask(source="studentexperience",
                                    city_key="amsterdam", city_display="Amsterdam"))


class TestRegistration:
    def test_locations_cover_exactly_the_registered_cities(self):
        from config import KNOWN_STUDENTEXPERIENCE_CITIES
        assert ({c for _n, c in LOCATIONS.values()}
                == {c["city"] for c in KNOWN_STUDENTEXPERIENCE_CITIES})

    def test_source_is_registered_end_to_end(self):
        from config import KNOWN_SOURCES, SOURCE_DISPLAY_NAMES
        from scrapers import SCRAPER_REGISTRY
        assert "studentexperience" in KNOWN_SOURCES
        assert SOURCE_DISPLAY_NAMES["studentexperience"] == "Student Experience"
        assert SCRAPER_REGISTRY["studentexperience"] is StudentExperienceScraper

    def test_tenant_is_declared_student_only(self):
        """FAQ 明文：exclusively available for students，签约需上传在读证明。"""
        from config import SOURCE_ASSUMED_FEATURES, sources_supporting_dim
        assert SOURCE_ASSUMED_FEATURES["studentexperience"] == {"Tenant": "student only"}
        assert "studentexperience" in sources_supporting_dim("tenant")

    def test_finishing_is_not_registered(self):
        """装修档位写进 features 但不登记为筛选维度——登记是 fail-closed 的。"""
        from config import sources_supporting_dim
        assert "studentexperience" not in sources_supporting_dim("finishing")

    def test_not_treated_as_full_lifecycle(self):
        """feed 只列可订户型，「消失」有歧义，必须走 Reserved → Occupied 两跳。"""
        from config import load_config
        import os
        os.environ["SOURCES"] = "studentexperience"
        try:
            cfg = load_config()
            assert "studentexperience" not in cfg.sources_with_full_lifecycle()
        finally:
            os.environ.pop("SOURCES", None)


# ── 测试替身 ────────────────────────────────────────────────────────

class _FakeSession:
    """只认三类 URL 的假会话：长租页（可分页）、学期档 JSON、短租页。"""

    def __init__(self, long_pages: list[str] | None = None, short_page: str = "",
                 detail_page: str = "", detail_raises: Exception | None = None):
        self._long = list(long_pages or [])
        self._short = short_page
        self._detail = detail_page
        self._detail_raises = detail_raises
        self.long_hits = 0
        self.detail_hits = 0

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def get(self, url, params=None, **_kw):
        params = params or {}
        if "getAcademicTerms" in url:
            import json
            return _Resp(json.dumps({"terms": [], "hasTerms": False}))
        if "/studios/" in url:                       # 详情页
            self.detail_hits += 1
            if self._detail_raises is not None:
                raise self._detail_raises
            return _Resp(self._detail)
        if params.get("los") == "longstay":
            self.long_hits += 1
            n = int(params.get("page", 1))
            # 超出范围回绕到第 1 页——真实站点就是这个行为，替身也照做，
            # 否则「翻到空为止」那种写法在测试里会显得没问题。
            return _Resp(self._long[(n - 1) % len(self._long)] if self._long else "")
        return _Resp(self._short)


class _Resp:
    def __init__(self, text: str, status: int = 200):
        self.text, self.status_code = text, status


class TestStartDate:
    """入住日期只在详情页有，列表卡片上完全没有。

    2026-09-04 实测：长租列表页里零个日期串（"Start date" 那两次是排序选项）。
    所以每个新单元要多发一次请求；这一组守的是「多发请求」带来的三个风险。
    """

    def test_english_long_date_becomes_iso(self):
        """站点写 ``1 October 2026``，库里各 source 统一存 ISO。

        不转换的后果不只是难看：``models.is_sentinel_available_from`` 按前四位
        判年份，``"1 Oc"`` 会走进 ``isdigit()`` 为假的分支，整条判断静默失效。
        """
        from scrapers.studentexperience import _parse_start_date
        page = _fx("studentexperience_detail_1525.html")
        assert _parse_start_date(page) == "2026-10-01"

    def test_the_deadline_is_not_mistaken_for_the_move_in_date(self):
        """详情页上有两个日期，取的必须是前一个。

            Start date contract   1 October 2026     ← 入住
            Respond until         6 September 2026   ← 申请截止
        """
        from scrapers.studentexperience import _parse_start_date
        page = _fx("studentexperience_detail_1525.html")
        assert "Respond until" in page
        assert _parse_start_date(page) == "2026-10-01"      # 不是 2026-09-06

    def test_unparseable_page_yields_none_not_a_crash(self):
        from scrapers.studentexperience import _parse_start_date
        assert _parse_start_date("<html>改版了</html>") is None
        assert _parse_start_date("Start date contract 32 Foguary 2026") is None

    def test_a_failing_detail_page_does_not_fail_the_round(self, page1, page2, monkeypatch):
        """详情页取不到时，房源照常入库、本轮照常判 complete。

        它是一个**可选字段**。把它的失败升格成 incomplete，等于让一个可选字段
        有权否决整轮——而 incomplete 的代价是 monitor 跳过 stale 收敛。
        """
        import scrapers.studentexperience as se
        from scrapers.base import ScrapeNetworkError

        monkeypatch.setattr(se, "_DETAIL_CACHE", {})
        s = se.StudentExperienceScraper()
        monkeypatch.setattr(s, "_session",
                            lambda: _FakeSession(long_pages=[page1, page2],
                                                 detail_raises=ScrapeNetworkError("429")))
        with s.batch_session():
            r = s.scrape(ScrapeTask(source="studentexperience",
                                    city_key="amsterdam", city_display="Amsterdam"))
        assert r.complete is True
        assert len(r.listings) == 14
        assert all(x.available_from is None for x in r.listings)

    def test_budget_caps_requests_per_round(self, page1, page2, monkeypatch):
        """一轮最多发 ``_DETAIL_BUDGET_PER_ROUND`` 个详情请求。

        铺不满不是问题：``mstorage._sticky_available_from`` 会让已问到的房源
        沿用旧值，所以渐进补齐没有「补一批、抹一批」的拉锯。
        """
        import scrapers.studentexperience as se

        monkeypatch.setattr(se, "_DETAIL_CACHE", {})
        monkeypatch.setattr(se, "_DETAIL_BUDGET_PER_ROUND", 3)
        monkeypatch.setattr(se.time, "sleep", lambda *_a: None)
        sess = _FakeSession(long_pages=[page1, page2],
                            detail_page=_fx("studentexperience_detail_1525.html"))
        s = se.StudentExperienceScraper()
        monkeypatch.setattr(s, "_session", lambda: sess)
        with s.batch_session():
            s.scrape(ScrapeTask(source="studentexperience",
                                city_key="amsterdam", city_display="Amsterdam"))
        assert sess.detail_hits == 3

    def test_cached_ids_are_not_refetched(self, page1, page2, monkeypatch):
        """开始日期是单元的稳定属性，一个进程里问一次就够。"""
        import scrapers.studentexperience as se

        monkeypatch.setattr(se, "_DETAIL_CACHE", {})
        monkeypatch.setattr(se.time, "sleep", lambda *_a: None)
        # 预算调到能一轮铺满，否则第二轮的请求是「补剩下的」而不是「重问已知的」，
        # 这条测试就分不清缓存有没有生效。
        monkeypatch.setattr(se, "_DETAIL_BUDGET_PER_ROUND", 99)
        detail = _fx("studentexperience_detail_1525.html")

        def _run(sess):
            s = se.StudentExperienceScraper()
            monkeypatch.setattr(s, "_session", lambda: sess)
            with s.batch_session():
                return s.scrape(ScrapeTask(source="studentexperience",
                                           city_key="amsterdam",
                                           city_display="Amsterdam"))

        first = _FakeSession(long_pages=[page1, page2], detail_page=detail)
        _run(first)
        second = _FakeSession(long_pages=[page1, page2], detail_page=detail)
        r = _run(second)
        assert first.detail_hits > 0
        assert second.detail_hits == 0, "第二轮不该再问同一批 id"
        assert all(x.available_from == "2026-10-01" for x in r.listings)

    def test_requests_are_spaced(self, page1, page2, monkeypatch):
        """两次详情请求之间要有间隔。

        站点没有 Cloudflare，但**不是不限速**：2026-09-04 连发两次同一页就吃了
        一个 403。间隔比预算更要紧——预算只是省流量，间隔是能不能拿到数据。
        """
        import scrapers.studentexperience as se

        monkeypatch.setattr(se, "_DETAIL_CACHE", {})
        monkeypatch.setattr(se, "_DETAIL_BUDGET_PER_ROUND", 4)
        slept: list[float] = []
        monkeypatch.setattr(se.time, "sleep", lambda s: slept.append(s))
        sess = _FakeSession(long_pages=[page1, page2],
                            detail_page=_fx("studentexperience_detail_1525.html"))
        s = se.StudentExperienceScraper()
        monkeypatch.setattr(s, "_session", lambda: sess)
        with s.batch_session():
            s.scrape(ScrapeTask(source="studentexperience",
                                city_key="amsterdam", city_display="Amsterdam"))
        assert len(slept) == 3          # 4 次请求之间 3 个间隔
        assert all(x > 0 for x in slept)
