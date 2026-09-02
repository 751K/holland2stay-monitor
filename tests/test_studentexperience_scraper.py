"""Student Experience scraper 的解析与完整性判据。

fixture 取自 2026-09-01 的真实页面：

    studentexperience_shortstay_loc2.html          Minervahaven 有货（2 个户型）
    studentexperience_longstay_empty.html          长租全站 0，计数块完整
    studentexperience_shortstay_loc3_noterm.html   Zuidas 无学期档，页面全空

第三份是这组测试里最要紧的一份：它长得**和「站点改版了」一模一样**——没有卡片、
没有计数块、连「暂无房源」的提示都没有。scraper 靠长租页的计数块把这两种情况分开，
下面 TestCompleteness 守的就是这条。
"""
from pathlib import Path

import pytest

from scrapers.studentexperience import (
    LOCATIONS,
    StudentExperienceScraper,
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


@pytest.fixture(scope="module")
def live_page() -> str:
    return _fx("studentexperience_shortstay_loc2.html")


@pytest.fixture(scope="module")
def empty_page() -> str:
    return _fx("studentexperience_longstay_empty.html")


@pytest.fixture(scope="module")
def noterm_page() -> str:
    return _fx("studentexperience_shortstay_loc3_noterm.html")


@pytest.fixture(scope="module")
def parsed(live_page):
    return [_parse_card(u, t, s, "12 Months (01-Oct-2026 - 30-Sep-2027)")
            for u, t, s in _split_cards(live_page)]


class TestCardSplit:
    def test_both_card_variants_are_collected(self, live_page):
        """主卡片与「其它户型」滑块里的紧凑卡片都要收。

        两者 class 不同（``has-popularity-header`` vs ``studio-compact``），但都是
        当前可订的户型——2026-09-01 实测 0 库存的楼盘连滑块都不渲染，所以滑块里
        出现即代表有货，不是全量目录。漏掉紧凑卡片会让 Essential studio 整个不
        进库。
        """
        cards = _split_cards(live_page)
        assert len(cards) == 2
        assert {t for _u, t, _s in cards} == {"10", "11"}

    def test_template_labels_are_not_mistaken_for_cards(self, live_page):
        """header 里的 ``data-label-want-studio`` 不是一张卡片。

        2026-09-01 就是数 "I want this studio" 的出现次数把可订数量数错过一次——
        那串文本在页面 header 的 data 属性里出现三次，与库存无关。卡片必须靠
        ``<a href=".../studio-types/N">`` 这个锚点来切。
        """
        assert live_page.count("I want this studio") > 2
        assert len(_split_cards(live_page)) == 2

    def test_zero_availability_page_has_no_cards(self, noterm_page, empty_page):
        assert _split_cards(noterm_page) == []
        assert _split_cards(empty_page) == []


class TestFieldExtraction:
    def test_main_card_fields(self, parsed):
        item = next(x for x in parsed if x.id == "se_11")
        assert item.name == "Amsterdam Minervahaven — Signature studio"
        assert item.price_raw == "€1.799"
        assert item.city == "Amsterdam"
        assert item.status == "Available to book"
        assert item.source == "studentexperience"
        assert item.url.endswith("/studio-types/11?los=shortstay&academicTermId=1678")

        fm = dict(f.split(": ", 1) for f in item.features)
        assert fm["Building"] == "Amsterdam Minervahaven"
        assert fm["Studio type"] == "Signature studio"
        assert fm["Area"] == "20,5-26 m²"
        assert fm["Address"] == "Moermanskkade 71a 1013 BC Amsterdam"
        assert fm["Units left"] == "2"

    def test_nested_spans_are_not_truncated(self, parsed):
        """押金与装修档位各自内嵌了一个同名标签，非贪婪正则会把它们截断。

        押金会截成「€1.750 deposit ·」（停在 ``studio-price-deposit-separator``
        的 ``</span>``），装修档位则整个丢失（挤在面积后面，由
        ``studio-spec-separator`` 隔开）。两者都要出现在通知里，所以解析器按开合
        标签配对而不是用 ``(.*?)</span>``。
        """
        item = next(x for x in parsed if x.id == "se_11")
        fm = dict(f.split(": ", 1) for f in item.features)
        assert fm["Deposit"] == "€1.750 deposit · fully refundable"
        assert fm["Finishing note"] == "Private & fully furnished"

    def test_travel_times_do_not_leak_into_specs(self, parsed):
        """「12 mins to nearest shops」那几行的 class 带前缀，会被块匹配收进来。

        它们不是面积、不是地址、也不是装修档位，混进任何一个字段都是错的。
        """
        item = next(x for x in parsed if x.id == "se_11")
        assert not any("mins to" in f for f in item.features)

    def test_travel_time_with_a_postcode_does_not_become_the_address(self, live_page):
        """通勤行里带邮编、且排在地址行**前面**时，地址会被它顶掉。

        这两个条件缺一不可，所以这条测试两件都做：
        - 把通勤文案换成带邮编的「5 mins to Amsterdam Centraal 1012 AB station」；
        - 把整个通勤块挪到地址行前面。

        只做第一件看不出差别——现有 DOM 里地址排在通勤之前，先到先得，过滤去掉
        也不影响结果。而这个解析器整套是按「顺序不保证、各认各的模式」写的
        （与 magis 同一策略），顺序一旦真的变了，这道过滤就是拦住错值的唯一一道。
        """
        import re
        page = live_page.replace(
            "<span>12 mins to nearest shops</span>",
            "<span>5 mins to Amsterdam Centraal 1012 AB station</span>")
        assert page != live_page, "文案变异未命中"

        m = re.search(r'<div class="studio-meta-travel-times">.*?</div>\s*</div>\s*</div>',
                      page, re.S)
        assert m, "找不到通勤块"
        block = m.group(0)
        page = page[:m.start()] + page[m.end():]
        anchor = page.index('<div class="studio-meta">')
        page = page[:anchor] + block + page[anchor:]

        item = next(x for u, t, s in _split_cards(page)
                    if (x := _parse_card(u, t, s)) and x.id == "se_11")
        fm = dict(f.split(": ", 1) for f in item.features)
        assert fm["Address"] == "Moermanskkade 71a 1013 BC Amsterdam"

    def test_compact_card_keeps_only_what_it_has(self, parsed):
        """紧凑卡片没有规格行，缺的字段就不写——不是写空值，也不是编一个。"""
        item = next(x for x in parsed if x.id == "se_10")
        assert item.price_raw == "€1.750"
        fm = dict(f.split(": ", 1) for f in item.features)
        assert fm["Studio type"] == "Essential studio"
        assert "Area" not in fm
        assert "Finishing note" not in fm

    def test_type_is_normalised_to_studio(self, parsed):
        """档位名（Signature / Essential）是价位分层，房型一律 Studio。"""
        for item in parsed:
            fm = dict(f.split(": ", 1) for f in item.features)
            assert fm["Type"] == "Studio"

    def test_area_range_is_kept_raw_so_parse_float_takes_the_lower_bound(self, parsed):
        """区间原样写，靠 parse_float 取下界喂给 fail-closed 的 min_area。

        写成上界或中值都会**错推**：用户要 ≥25 m² 时，不该因为这个户型里恰好有
        几间 26 m² 就把整个（最小 20,5 m²）户型推给他。
        """
        from models import parse_float
        item = next(x for x in parsed if x.id == "se_11")
        fm = dict(f.split(": ", 1) for f in item.features)
        assert parse_float(fm["Area"]) == 20.5


class TestUnknownResidence:
    def test_unregistered_building_is_dropped_not_guessed(self, live_page):
        """楼盘名不在 LOCATIONS 里就丢掉，不猜城市。

        猜错会把房源分派给错误的 ScrapeTask——用户按城市订阅，就会收到不该收的。
        站点新开一处时宁可这一处暂时不进库，也不要静默投错城市。
        """
        page = live_page.replace("Amsterdam Minervahaven", "Rotterdam Nieuwehaven")
        cards = _split_cards(page)
        assert cards, "改名不该影响切卡片"
        assert all(_parse_card(u, t, s) is None for u, t, s in cards)


class TestCompleteness:
    def test_complex_counts_parse_from_the_empty_longstay_page(self, empty_page):
        """计数块在**没货时也渲染**——这正是它能当结构探针的原因。"""
        counts = _parse_complex_counts(empty_page)
        assert counts == {
            "Amsterdam Amstel": 0, "Amsterdam NDSM": 0,
            "Amsterdam Zuidas": 0, "Leiden": 0,
        }

    def test_shortstay_empty_page_yields_no_counts(self, noterm_page):
        """短租页没有计数块，所以它单独一份判不出「空」还是「改版」。"""
        assert _parse_complex_counts(noterm_page) == {}

    def test_missing_count_block_marks_the_round_incomplete(self, monkeypatch):
        """读不到计数块 = 站点改版 → incomplete，让 monitor 跳过 stale 收敛。

        不这么做的后果不是「少推几条」，而是**整批存量房源被收敛成 Occupied 并发
        一批假的下架通知**。
        """
        s = StudentExperienceScraper()
        monkeypatch.setattr(s, "_session", lambda: _FakeSession(long_page="<html>改版了</html>"))
        with s.batch_session():
            r = s.scrape(ScrapeTask(source="studentexperience",
                                    city_key="amsterdam", city_display="Amsterdam"))
        assert r.complete is False and r.listings == []

    def test_counts_above_zero_with_no_cards_marks_incomplete(self, monkeypatch, empty_page):
        """计数说有货、却一张卡片都没解析出来 → 卡片类名变了。

        这是「计数块还在、卡片结构变了」的情形；只守计数块守不住它。
        """
        page = _bump_complex_count(empty_page, "Leiden", 3)
        # 先确认变异真的咬住了。上一版这里写的是 ``page.replace("Leiden 0", …)``，
        # 而真实标记是 ``<label for="complex-Leiden">…</label><span
        # class="amount">0</span>``——替换一次都没命中，整条测试空过。
        assert _parse_complex_counts(page)["Leiden"] == 3
        assert _split_cards(page) == []

        s = StudentExperienceScraper()
        monkeypatch.setattr(s, "_session", lambda: _FakeSession(long_page=page))
        with s.batch_session():
            r = s.scrape(ScrapeTask(source="studentexperience",
                                    city_key="leiden", city_display="Leiden"))
        assert r.complete is False and r.listings == []


class TestTransportErrors:
    def test_proxy_failure_is_recognisable_as_one(self, monkeypatch):
        """代理故障必须包成 ScrapeNetworkError，且消息保留原文。

        dispatcher 只在 ``except ScrapeNetworkError`` 那一支里调
        ``is_proxy_error``（scrapers/__init__.py:272）。裸的 curl_cffi ProxyError
        会掉进后面的通用 ``except Exception``，被记成「未预期异常」——本 source
        于是永远不触发代理冷却，只能等别的 source 去发现代理坏了。

        2026-09-02 生产实测：代理 402 欠费，同一轮里 xior 报「代理已确认故障并
        进入冷却」，本 source 报的是「未预期异常，已隔离该任务」。
        """
        from scrapers.base import ScrapeNetworkError, is_proxy_error

        class _ProxyError(Exception):
            pass

        class _Boom:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def get(self, *_a, **_kw):
                raise _ProxyError(
                    "Failed to perform, curl: (56) CONNECT tunnel failed, "
                    "response 402.")

        s = StudentExperienceScraper()
        monkeypatch.setattr(s, "_session", lambda: _Boom())
        with pytest.raises(ScrapeNetworkError) as ei:
            with s.batch_session():
                s.scrape(ScrapeTask(source="studentexperience",
                                    city_key="amsterdam", city_display="Amsterdam"))
        assert is_proxy_error(ei.value), (
            f"代理故障没被认出来：{ei.value!r}——冷却机制不会被触发")

    def test_http_error_does_not_become_zero_listings(self, monkeypatch):
        """站点 5xx 必须上抛，**绝不能**变成「本轮 0 条房源」。

        读成 0 条的后果不是少推几条，是整批存量被 stale 收敛判成 Occupied 并发一
        批假下架通知。2026-09-02 站点真的 500 过一次，这条路径当时被实弹验证过。
        """
        from scrapers.base import ScrapeNetworkError

        class _Down:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def get(self, *_a, **_kw):
                return _Resp("<html>500</html>", status=500)

        s = StudentExperienceScraper()
        monkeypatch.setattr(s, "_session", lambda: _Down())
        with pytest.raises(ScrapeNetworkError, match="500"):
            with s.batch_session():
                s.scrape(ScrapeTask(source="studentexperience",
                                    city_key="amsterdam", city_display="Amsterdam"))


class TestCitySplit:
    def test_listings_are_dispatched_by_city(self, monkeypatch, live_page, empty_page):
        s = StudentExperienceScraper()
        monkeypatch.setattr(
            s, "_session",
            lambda: _FakeSession(long_page=empty_page, short_page=live_page))
        with s.batch_session():
            ams = s.scrape(ScrapeTask(source="studentexperience",
                                      city_key="amsterdam", city_display="Amsterdam"))
            lei = s.scrape(ScrapeTask(source="studentexperience",
                                      city_key="leiden", city_display="Leiden"))
        assert {x.id for x in ams.listings} == {"se_10", "se_11"}
        assert lei.listings == []
        assert ams.complete and lei.complete

    def test_dispatch_uses_display_name_not_key(self, monkeypatch, live_page, empty_page):
        """按 ``city_display`` 分派，不是 ``city_key``。

        本平台的 key 恰好是 name 的小写，两者怎么比都一样——所以这条用一个 key 与
        name 不同的 task 来分辨。这不是假设：Magis 的
        ``'s-Hertogenbosch`` / ``s-hertogenbosch`` 就差一个撇号和大小写，
        ``Listing.city`` 存的一直是 display 那一侧。
        """
        s = StudentExperienceScraper()
        monkeypatch.setattr(
            s, "_session",
            lambda: _FakeSession(long_page=empty_page, short_page=live_page))
        with s.batch_session():
            r = s.scrape(ScrapeTask(source="studentexperience",
                                    city_key="ams-001", city_display="Amsterdam"))
        assert {x.id for x in r.listings} == {"se_10", "se_11"}

    def test_batch_fetches_once_for_all_cities(self, monkeypatch, live_page, empty_page):
        """两个城市共用一轮抓取——分两轮抓会把请求翻倍，且两轮之间库存可能变化。"""
        sess = _FakeSession(long_page=empty_page, short_page=live_page)
        s = StudentExperienceScraper()
        monkeypatch.setattr(s, "_session", lambda: sess)
        with s.batch_session():
            for city in ("Amsterdam", "Leiden"):
                s.scrape(ScrapeTask(source="studentexperience",
                                    city_key=city.lower(), city_display=city))
        assert sess.long_hits == 1


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
        """FAQ 明文：exclusively available for students，签约需上传在读证明。

        不登记的后果是这个维度对本平台整体 fail-open——勾了「仅学生」的用户会
        收到本该匹配的房源没错，但勾了别的身份的用户也会收到。
        """
        from config import SOURCE_ASSUMED_FEATURES, sources_supporting_dim
        assert SOURCE_ASSUMED_FEATURES["studentexperience"] == {"Tenant": "student only"}
        assert "studentexperience" in sources_supporting_dim("tenant")

    def test_finishing_is_not_registered(self):
        """规格行只在主卡片上有；登记 fail-closed 维度会把紧凑卡片那几条全滤掉。"""
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


class _FakeSession:
    """只认三类 URL 的假会话：长租页、学期档 JSON、短租页。"""

    def __init__(self, long_page: str = "", short_page: str = ""):
        self._long, self._short = long_page, short_page
        self.long_hits = 0

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def get(self, url, params=None, **_kw):
        params = params or {}
        if "getAcademicTerms" in url:
            loc = url.rstrip("/").rsplit("/", 1)[-1]
            terms = ([{"yardiAcademicTermIdValue": "1678",
                       "academicTermName": "12 Months (01-Oct-2026 - 30-Sep-2027)"}]
                     if loc == "2" and self._short else [])
            import json
            return _Resp(json.dumps({"terms": terms, "hasTerms": bool(terms)}))
        if params.get("los") == "longstay":
            self.long_hits += 1
            return _Resp(self._long)
        return _Resp(self._short)


class _Resp:
    def __init__(self, text: str, status: int = 200):
        self.text, self.status_code = text, status
