"""一个 source 的解析 bug 只该影响那个 source。

2026-09-02 一次性发现四条都违反这条：

  magis      城市认不出 → 同一条房源被每个城市 task 各收一次 → diff() 的
             UNIQUE 冲突 → **整个事务回滚 → 本轮所有平台都不入库、不通知**
  plaza      一行 netRent="n.v.t." → float() 抛出 → 整源每轮失败
  xior       floorplans.aspx 改版 → 返回空集而非 None → 全部真房源被判 Occupied
             且标记「已核实」，静默漏推
  holland2stay  详情补齐的 except Exception 吞掉 BlockedError → 被挡之后继续打，
             一个批次可以触发几十次浏览器重建 + 换 IP

前三条的共同点是**「认不出」被当成了一个确定的答案**（哪个城市都算 / 一个都订不了），
而不是「不知道」。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from models import Listing

FIXTURES = Path(__file__).parent / "fixtures"


def _code(fn) -> str:
    """函数源码，**剥掉注释**。

    这个文件里的断言都是「源码里不许出现 / 必须出现某段写法」。而解释性注释里
    常常原样引用那段坏写法——不剥的话咬中的是注释而不是代码。今天这个坑踩了三次
    （覆盖横幅的 .alert、magis 的城市判断、这里的 except Exception），所以提成
    一个共用函数。
    """
    import inspect
    import re
    src = inspect.getsource(fn)
    return "\n".join(re.sub(r"#.*$", "", ln) for ln in src.split("\n"))


def _L(lid: str, city: str, source: str = "magis") -> Listing:
    return Listing(id=lid, name="X", status="Available to book", price_raw="€1",
                   available_from=None, features=[], url="", city=city, source=source)


class TestDuplicateIdsDoNotLoseTheRound:
    """diff() 收到重复 id 时去重，而不是让整轮回滚。"""

    def test_duplicates_do_not_roll_back_everything(self, tmp_path):
        """一条重复 id 曾经让**所有平台**的本轮结果一起消失。

        ``existing`` 是循环前的一次性快照，第二条同 id 查不到自己刚插的行，
        于是再 INSERT → UNIQUE 冲突 → ``with self._conn`` 整个事务回滚。
        """
        from storage import Storage

        st = Storage(tmp_path / "t.db")
        fresh = [_L("dup", "")] * 5 + [_L("ok", "Eindhoven", source="holland2stay")]
        new, _ = st.diff(fresh)

        got = {r[0] for r in st.conn.execute("SELECT id FROM listings")}
        assert got == {"dup", "ok"}, f"入库结果不对：{got}"
        assert len(new) == 2

    def test_the_duplicate_is_reported(self, tmp_path, caplog):
        """去重要留痕：同一轮出现同 id 本身就是 scraper 的 bug，静默合并会藏起根因。"""
        from storage import Storage

        st = Storage(tmp_path / "t.db")
        with caplog.at_level(logging.WARNING):
            st.diff([_L("dup", "")] * 3)
        assert "重复 id" in caplog.text and "magis" in caplog.text

    def test_normal_rounds_are_untouched(self, tmp_path):
        from storage import Storage

        st = Storage(tmp_path / "t.db")
        new, _ = st.diff([_L("a", "Eindhoven"), _L("b", "Tilburg")])
        assert len(new) == 2


class TestMagisCityDispatch:
    """城市认不出 ≠ 哪个城市都算。"""

    def _cards_with_unknown_city(self):
        from scrapers.magis import _split_cards
        page = (FIXTURES / "magis_for_rent_all.html").read_text(
            encoding="utf-8", errors="replace")
        # 把已知城市替换成一个没登记的，模拟站点新开一城
        return page.replace("Eindhoven", "Groningen")

    def test_unknown_city_is_not_emitted_for_every_task(self):
        """严格相等：认不出城市的卡片不进任何一个 task，而不是进所有 task。"""
        from scrapers.base import ScrapeTask
        from scrapers.magis import MagisScraper

        page = self._cards_with_unknown_city()
        s = MagisScraper()
        s._fetch = lambda: page

        ids_per_city = {}
        with s.batch_session():
            for city in ("Tilburg", "Rijswijk", "Amersfoort"):
                r = s.scrape(ScrapeTask(source="magis", city_key=city.lower(),
                                        city_display=city))
                ids_per_city[city] = {x.id for x in r.listings}

        # 同一个 id 不该出现在多个城市里
        seen: set[str] = set()
        for city, ids in ids_per_city.items():
            overlap = seen & ids
            assert not overlap, f"{city} 与其它城市共享了 id：{overlap}"
            seen |= ids

    def test_unknown_city_is_logged(self, caplog):
        """丢掉就要说一声——否则站点新开城市时那批房源凭空消失。"""
        from scrapers.base import ScrapeTask
        from scrapers.magis import MagisScraper

        s = MagisScraper()
        s._fetch = lambda: self._cards_with_unknown_city()
        with caplog.at_level(logging.WARNING):
            with s.batch_session():
                s.scrape(ScrapeTask(source="magis", city_key="tilburg",
                                    city_display="Tilburg"))
        assert "KNOWN_MAGIS_CITIES" in caplog.text

    def test_uses_strict_equality(self):
        """钉住写法本身：``if item.city and item.city != ...`` 是那个 bug 的形状。

        断言前先剥注释——那段说明里原样引用了坏写法，不剥的话咬中的是自己的注释。
        （这个坑今天已经踩过两次：test_uses_a_dedicated_class_not_alert、
        以及 magis 早先那批 grep 源码的测试。）
        """
        from scrapers.magis import MagisScraper

        assert "if item.city and item.city !=" not in _code(MagisScraper.scrape)


class TestPlazaMalformedRow:
    def test_non_numeric_rent_does_not_kill_the_source(self):
        """一行 netRent="n.v.t." 曾经让整个 Plaza 每轮失败。"""
        from scrapers.plaza import _parse_object

        item = _parse_object({
            "dwellingType": {"categorie": "woning"}, "land": {"id": "524"},
            "id": 1, "city": {"name": "Utrecht"}, "totalRent": 800,
            "netRent": "n.v.t.", "street": "X", "houseNumber": 1,
        })
        assert item is not None
        fm = dict(f.split(": ", 1) for f in item.features)
        assert "Net rent" not in fm, "认不出的值不该硬写进去"

    def test_one_bad_row_is_dropped_not_fatal(self, caplog):
        """更兜底的一层：任何一行抛异常都只丢那一行。

        字段选 ``doelgroepen``（正常是 list，这里给 int）而不是 ``totalRent``：
        后者现在被 ``_num`` 优雅处理，根本走不到 try，用它当变异目标会让这条测试
        空过——第一版就是那样。这里要的是一个**真的会抛**的形状。
        """
        from scrapers.plaza import PlazaScraper, _parse_object

        bad = {
            "dwellingType": {"categorie": "woning"}, "land": {"id": "524"},
            "id": 999_999, "city": {"name": "Utrecht"}, "totalRent": 800,
            "street": "Y", "houseNumber": 2,
            "doelgroepen": 5,                      # 正常是 list → 迭代时 TypeError
        }
        with pytest.raises(TypeError):
            _parse_object(bad)                     # 前提：它确实会抛

        payload = json.loads(
            (FIXTURES / "plaza_getallobjects.json").read_text(encoding="utf-8"))
        payload["result"] = [bad] + list(payload["result"])
        with caplog.at_level(logging.WARNING):
            items, complete = PlazaScraper()._parse_all(payload)
        assert complete, "一行畸形数据不该让整源判为不完整"
        assert len(items) >= 48, f"其余房源也丢了：{len(items)}"


class TestXiorStructureProbe:
    """这道闸门是唯一没有结构探针的——现在有了。"""

    def test_unrecognised_page_returns_none_not_empty_set(self):
        """None = 「没查成，信 feed」；空集 = 「查过了，一个都订不了」。

        HTTP 200 但页面改版时返回空集，会让**全部真房源**被判 Occupied 且标记为
        已核实——没有异常、没有告警，表现就是「今天没房」。
        """
        from scrapers.xior import parse_bookable_floorplan_ids as parse

        assert parse("<html>maintenance</html>") is None
        assert parse("") is None

    def test_a_real_empty_result_is_still_an_empty_set(self):
        """有 tile 但都订不了是**真实的** 0，必须保留——否则闸门等于关掉。"""
        from scrapers.xior import parse_bookable_floorplan_ids as parse

        page = ('data-selenium-id="FloorPlanAvailability"'
                '<button data-selenium-id="ApplyNow">Rented Out</button>'
                'floorPlans=123')
        assert parse(page) == set()

    def test_return_type_allows_none(self):
        import inspect

        from scrapers import xior

        sig = inspect.signature(xior.parse_bookable_floorplan_ids)
        assert "Optional" in str(sig.return_annotation) or "None" in str(
            sig.return_annotation)


class TestH2SEnrichStopsWhenBlocked:
    def test_blocked_error_has_its_own_branch(self):
        """BlockedError 落进 except Exception 的话会被当成「这一条不巧失败了」，
        于是每条都重试——一个 60 条的批次可以触发几十次浏览器重建 + 换 IP。"""
        from scrapers import holland2stay

        src = _code(holland2stay._enrich)
        assert "except BlockedError" in src, "BlockedError 没有独立分支"
        i = src.index("except BlockedError")
        j = src.index("except Exception")
        assert i < j, "BlockedError 必须排在 except Exception 前面，否则接不到"
        # 收手而不是 continue
        block = src[i:j]
        assert "break" in block and "stopped = True" in block


class TestHungRendererDoesNotStallTheLoop:
    """渲染器卡死时，本轮其余 source 仍要走完。

    ``page.evaluate`` 在 Playwright 里没有 timeout 参数（它等的是 JS promise，
    ``set_default_timeout`` 也管不到），``page.content()`` 同样没有。一个 wedged
    页面会让浏览器线程永远停住，而 monitor 侧的 ``await run_in_executor`` 原先没有
    上限——run_once 就不返回了，表现是 last_round_at 不断变老，与「进程挂了」无从
    区分。
    """

    def test_dispatch_has_a_wall_clock_timeout(self):
        import inspect

        import monitor

        src = _code(monitor._dispatch_scrape_tasks_async)
        assert "asyncio.wait_for" in src, "await 没有上限，一个源卡死会挂住整个循环"
        assert "_SOURCE_DISPATCH_TIMEOUT_SEC" in src

    def test_timeout_becomes_a_source_failure_not_a_hang(self):
        """超时要变成「这个 source 本轮失败」，交给既有的隔离/熔断路径处置。"""
        import asyncio

        import monitor
        from scrapers.base import ScrapeNetworkError, ScrapeTask

        async def _run():
            loop = asyncio.get_running_loop()
            monkey = monitor._SOURCE_DISPATCH_TIMEOUT_SEC
            monitor._SOURCE_DISPATCH_TIMEOUT_SEC = 0.2
            try:
                import scrapers
                orig = scrapers.dispatch_scrape_tasks

                def _hang(*_a, **_kw):
                    import time
                    time.sleep(5)          # 模拟 wedged 渲染器

                monitor.dispatch_scrape_tasks = _hang
                try:
                    with pytest.raises(ScrapeNetworkError, match="超时"):
                        await monitor._dispatch_scrape_tasks_async(
                            loop,
                            [ScrapeTask(source="xior", city_key="a",
                                        city_display="A")],
                        )
                finally:
                    monitor.dispatch_scrape_tasks = orig
            finally:
                monitor._SOURCE_DISPATCH_TIMEOUT_SEC = monkey

        asyncio.run(_run())

    def test_page_gets_a_default_timeout(self):
        """page.content() 那一半由页面级默认超时兜住。"""
        import inspect

        import browser_fetcher

        src = _code(browser_fetcher.BrowserFetcher._ensure_browser) \
            if hasattr(browser_fetcher.BrowserFetcher, "_ensure_browser") else ""
        whole = (Path(browser_fetcher.__file__).read_text(encoding="utf-8"))
        assert "set_default_timeout" in whole, "页面没有默认超时"
        assert "_PAGE_DEFAULT_TIMEOUT_MS" in whole
