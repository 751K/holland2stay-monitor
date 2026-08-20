"""H2S 分层抓取：高频只查「可订类」，低频才做含 Reserved 的全量。

为什么要分层
------------
2026-08-18 H2S 上线 GraphQL operation 白名单后，查询字段集被锁死（见
h2s_gql.py），响应体没得裁。能动的只剩「查什么」。实测两城合计、线上真实字节：

    只查 可订 + 抽签 + 即将上线      2.2 KB/轮
    再加上 Reserved               292.2 KB/轮

贵的是那批已被预订的房源——每轮完整拉一遍，而 Reserved 状态几乎不动。

两个必须守住的性质
------------------
1. **高频轮不能算完整扫描。** 它看不见 Reserved，若标成 complete=True，那批
   房源会被 stale 收敛判成「已下架」清掉。
2. **分层决策按批次做，不按城市。** 实现时就踩过：在 _plan_scan 里直接推进
   计时器，导致一轮里第一个城市消耗掉「该全量」的标记，其余城市统统被降级
   ——那一轮只有第一个城市是全量的。
"""
from __future__ import annotations

import pytest

from scrapers.holland2stay import (
    _ARCHIVE_STATUSES,
    _FRESH_STATUSES,
    _FULL_SCAN_INTERVAL,
    HollandStayScraper,
)

CONFIGURED = ["179", "336", "6203"]      # 生产配置：可订 / 抽签 / Reserved


@pytest.fixture
def scraper():
    return HollandStayScraper()


class TestTierChoice:
    def test_first_batch_is_full(self, scraper):
        scraper._begin_batch()
        ids, is_full = scraper._plan_scan(CONFIGURED)
        assert is_full
        assert set(ids) == set(CONFIGURED)

    def test_next_batch_drops_to_fresh(self, scraper):
        scraper._begin_batch()
        scraper._plan_scan(CONFIGURED)
        scraper._note_full_scan_done()       # 全量真的跑完了，计时器才推进
        scraper._begin_batch()
        ids, is_full = scraper._plan_scan(CONFIGURED)
        assert not is_full
        assert set(ids) == {"179", "336"}, "高频层不该带上 Reserved"

    def test_full_scan_returns_after_the_interval(self, scraper, monkeypatch):
        scraper._begin_batch()
        assert scraper._plan_scan(CONFIGURED)[1]
        scraper._note_full_scan_done()

        import scrapers.holland2stay as h2s
        base = [0.0]
        monkeypatch.setattr(h2s.time, "monotonic", lambda: base[0])
        scraper._last_full_scan_at = 0.0

        base[0] = _FULL_SCAN_INTERVAL - 1
        scraper._begin_batch()
        assert not scraper._plan_scan(CONFIGURED)[1], "还没到点就做全量了"

        base[0] = _FULL_SCAN_INTERVAL + 1
        scraper._begin_batch()
        assert scraper._plan_scan(CONFIGURED)[1], "到点了却没做全量"


class TestEveryCityInARoundGetsTheSameTier:
    """一轮里每个城市各调一次 scrape()，层级必须一致。

    回归：曾经在 _plan_scan 里直接推进计时器，Amsterdam 消耗掉「该全量」，
    Eindhoven 就被降级成高频层——那一轮 Eindhoven 抓到 0 条且 complete=False，
    而它本该是 42 条的全量。
    """

    def test_all_cities_share_the_batch_decision(self, scraper):
        scraper._begin_batch()
        decisions = [scraper._plan_scan(CONFIGURED)[1] for _ in range(4)]
        assert decisions == [True] * 4, f"同一批次里层级不一致: {decisions}"

    def test_fresh_batch_is_uniform_too(self, scraper):
        scraper._begin_batch()
        scraper._plan_scan(CONFIGURED)
        scraper._note_full_scan_done()
        scraper._begin_batch()
        decisions = [scraper._plan_scan(CONFIGURED)[1] for _ in range(4)]
        assert decisions == [False] * 4


class TestRespectsUserConfig:
    """分层不能越过用户配置去查他没要的状态。"""

    def test_never_queries_unconfigured_statuses(self, scraper):
        scraper._begin_batch()
        for configured in (["179"], ["179", "336"], CONFIGURED, ["6203"]):
            scraper._begin_batch()
            ids, _ = scraper._plan_scan(list(configured))
            assert set(ids) <= set(configured), (
                f"配置 {configured} 却查了 {ids}"
            )

    def test_no_archive_status_means_always_full(self, scraper):
        """用户没要 Reserved 这类 → 没有可省的，每轮都是全量。"""
        scraper._begin_batch()
        scraper._plan_scan(["179", "336"])
        scraper._begin_batch()          # 第二批，按理该降级
        ids, is_full = scraper._plan_scan(["179", "336"])
        assert is_full, "没有可省的状态时不该降级——那会白白丢掉完整扫描"
        assert set(ids) == {"179", "336"}

    def test_only_archive_status_means_always_full(self, scraper):
        """用户只要 Reserved：没有高频层可走，退回全量，否则永远查不到东西。"""
        scraper._begin_batch()
        scraper._plan_scan(["6203"])
        scraper._begin_batch()
        ids, is_full = scraper._plan_scan(["6203"])
        assert is_full
        assert set(ids) == {"6203"}


class TestStatusSetsAreDisjoint:
    def test_no_status_in_both_tiers(self):
        assert not (set(_FRESH_STATUSES) & set(_ARCHIVE_STATUSES))

    def test_bookable_is_in_the_fresh_tier(self):
        """179「可订」必须在高频层——用户要的通知全靠它。"""
        assert "179" in _FRESH_STATUSES
        assert "336" in _FRESH_STATUSES, "抽签也是可申请的，别漏"

    def test_reserved_is_archived(self):
        """Reserved 是流量大头，必须在低频层。"""
        assert "6203" in _ARCHIVE_STATUSES


class TestFreshRoundIsNeverComplete:
    """高频轮必须 complete=False——这是分层里最危险的一条。

    它只查「可订类」，看不见 Reserved。若标成完整扫描，stale 收敛会把那批
    Reserved 房源判成「已下架」全部清掉：库里几十条房源凭空消失，而日志上
    只会显示一次正常的完整扫描。
    """

    @staticmethod
    def _run(scraper, monkeypatch):
        """跑一次 scrape()，底层抓取假装成功且自称完整。"""
        import scrapers.holland2stay as h2s
        from scrapers.base import ScrapeTask

        monkeypatch.setattr(h2s, "_scrape_city_pages",
                            lambda *a, **k: ([], True))

        class _FakeFetcher:
            def __init__(self, headless=True, profile=None): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def ensure_initialized(self): pass

        monkeypatch.setattr(h2s, "BrowserFetcher", _FakeFetcher)
        scraper._fetcher = _FakeFetcher()
        return scraper.scrape(ScrapeTask(
            source="holland2stay", city_key="29", city_display="Eindhoven",
            extra={"availability_ids": CONFIGURED},
        ))

    def test_full_round_may_be_complete(self, scraper, monkeypatch):
        scraper._begin_batch()
        assert self._run(scraper, monkeypatch).complete is True

    def test_fresh_round_is_forced_incomplete(self, scraper, monkeypatch):
        scraper._begin_batch()
        self._run(scraper, monkeypatch)      # 消耗掉第一次全量
        scraper._begin_batch()
        result = self._run(scraper, monkeypatch)
        assert result.complete is False, (
            "高频轮自称完整扫描 —— stale 收敛会清空所有 Reserved 房源"
        )


class TestTimerAdvancesOnlyAfterTheWorkIsDone:
    """全量扫描的计时器要在**干完活之后**推进，不是在决定要干活的时候。

    原实现在 ``_begin_batch()`` 里当场 ``_last_full_scan_at = now``——批次随后
    403 / 熔断 / 代理挂掉时，那次全量一条数据都没拿到，计时器却已经走了：
    下一次全量要再等 30 分钟。H2S 熔断退避最长 6 小时，期间 Reserved 那批
    房源的状态收敛可以停摆很久，而日志上看不出「本该全量的那轮没成」。

    典型的**先记账后干活**。
    """

    @staticmethod
    def _scrape(scraper, monkeypatch, *, complete=True, boom=None):
        import scrapers.holland2stay as h2s
        from scrapers.base import ScrapeTask

        def _pages(*a, **k):
            if boom is not None:
                raise boom
            return [], complete

        monkeypatch.setattr(h2s, "_scrape_city_pages", _pages)
        monkeypatch.setattr(h2s, "_enrich", lambda *a, **k: 0)

        class _FakeFetcher:
            def __init__(self, headless=True, profile=None): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def ensure_initialized(self): pass

        monkeypatch.setattr(h2s, "BrowserFetcher", _FakeFetcher)
        scraper._fetcher = _FakeFetcher()
        return scraper.scrape(ScrapeTask(
            source="holland2stay", city_key="29", city_display="Eindhoven",
            extra={"availability_ids": CONFIGURED},
        ))

    def test_planning_alone_does_not_consume_the_interval(self, scraper):
        """光是「决定要做全量」不算数——没抓到数据，额度不该被扣掉。"""
        scraper._begin_batch()
        assert scraper._plan_scan(CONFIGURED)[1]
        scraper._begin_batch()
        assert scraper._plan_scan(CONFIGURED)[1], (
            "只是规划了一次全量，计时器就被推进了"
        )

    def test_completed_full_scan_advances_the_timer(self, scraper, monkeypatch):
        scraper._begin_batch()
        assert self._scrape(scraper, monkeypatch, complete=True).complete
        scraper._begin_batch()
        assert not scraper._plan_scan(CONFIGURED)[1], "全量跑完了却没推进计时器"

    def test_failed_full_scan_does_not_advance_the_timer(self, scraper, monkeypatch):
        """403 / 网络失败 —— 一条 Reserved 都没看见，下一轮必须马上重试全量。"""
        from scrapers.base import BlockedError

        scraper._begin_batch()
        with pytest.raises(BlockedError):
            self._scrape(scraper, monkeypatch, boom=BlockedError("403"))
        scraper._begin_batch()
        assert scraper._plan_scan(CONFIGURED)[1], (
            "全量失败了却把额度算掉，下次全量要再等 30 分钟"
        )

    def test_incomplete_full_scan_does_not_advance_the_timer(self, scraper, monkeypatch):
        """抓到一半（缺 total_pages / 解析失败率超标）同样不算数。"""
        scraper._begin_batch()
        assert not self._scrape(scraper, monkeypatch, complete=False).complete
        scraper._begin_batch()
        assert scraper._plan_scan(CONFIGURED)[1]

    def test_one_city_completing_is_enough(self, scraper, monkeypatch):
        """一批里只要有城市跑完了全量，这份开销就已经付过了。

        要求「全部城市都完成」会让一个长期坏掉的城市把整批钉死在每轮全量上。
        """
        scraper._begin_batch()
        self._scrape(scraper, monkeypatch, complete=True)     # 城市 A 成功
        self._scrape(scraper, monkeypatch, complete=False)    # 城市 B 不完整
        scraper._begin_batch()
        assert not scraper._plan_scan(CONFIGURED)[1]

    def test_batch_that_planned_full_but_got_nothing_is_reported(
        self, scraper, monkeypatch, caplog,
    ):
        """计划了全量却一条没成，必须吵一声——否则「Reserved 长期没被看到」
        在日志上完全不可见。"""
        import logging
        import scrapers.holland2stay as h2s

        monkeypatch.setattr(scraper, "_ensure_browser", lambda: None)
        with caplog.at_level(logging.WARNING, logger=h2s.logger.name):
            with scraper.batch_session():
                pass          # 所有 task 都被 dispatcher 隔离掉了，一条没跑成
        assert "全量" in caplog.text, f"没报出来: {caplog.text!r}"
