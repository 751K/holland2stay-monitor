"""「这栋楼一个单元都没有」要连着看到几轮才算数。

`_looks_like_availability_panel` 能挡住「这压根不是单元面板」，挡不住「是面板，
可这一次的内容不对」——两种情况的 HTTP 状态、页面结构、长度都一样，从单次
响应里区分不出来。

而 ``complete=True`` 的含义是「这轮扫全了，没见到 = 真没了」，
``mark_stale_listings`` 完全信任它。所以一次内容异常的空响应就够让整栋楼的
存量 listing 走上收敛路径。

现在没出事，是因为 OurDomain 的房源实际寿命是 0.2–3.1 小时（2026-08-04 查线上
数据：一批出现，然后一条条被订走），而老化阈值是 7 天——差着约 800 倍的余量，
一次误判来不及产生后果。**阈值在替这个漏洞兜底。** 一旦阈值调到和真实寿命同
量级，兜底就没了，所以这道闸是缩短阈值的前置条件。

盯四件事：
1. 单轮空结果不算数（complete=False → monitor 跳过这栋楼的收敛）；
2. 连够 N 轮才算数；
3. 抓到任何单元立刻清零——不能让「4 个 → 0 个 → 4 个」攒出一次误判；
4. 每栋楼各记各的，别互相顶掉。
"""
from __future__ import annotations

import pytest

from scrapers.base import ScrapeTask
from scrapers.ourdomain import (
    _DEFAULT_ZERO_ROUNDS_TO_CONFIRM,
    _ZERO_ROUND_STATE,
    _confirm_empty_result,
    _zero_rounds_to_confirm,
)


@pytest.fixture(autouse=True)
def _clean_state():
    _ZERO_ROUND_STATE.clear()
    yield
    _ZERO_ROUND_STATE.clear()


class TestEmptyNeedsConfirmation:
    def test_first_empty_round_is_not_believed(self):
        assert _confirm_empty_result("ourdomain:diemen", 0) is False

    def test_believed_after_enough_consecutive_rounds(self):
        key = "ourdomain:diemen"
        need = _zero_rounds_to_confirm()
        for _ in range(need - 1):
            assert _confirm_empty_result(key, 0) is False
        assert _confirm_empty_result(key, 0) is True

    def test_stays_believed_once_confirmed(self):
        """确认之后不该再回到「不确定」——楼真空了就是空了。"""
        key = "ourdomain:diemen"
        for _ in range(_zero_rounds_to_confirm() + 3):
            _confirm_empty_result(key, 0)
        assert _confirm_empty_result(key, 0) is True

    def test_any_unit_resets_the_counter(self):
        """「4 个 → 0 个 → 4 个 → 0 个」不该攒成一次确认。"""
        key = "ourdomain:diemen"
        need = _zero_rounds_to_confirm()
        for _ in range(need - 1):
            _confirm_empty_result(key, 0)
        assert _confirm_empty_result(key, 4) is True      # 房源回来了
        assert _confirm_empty_result(key, 0) is False, "计数没清零"

    def test_non_empty_is_always_believed(self):
        assert _confirm_empty_result("ourdomain:diemen", 1) is True

    def test_buildings_are_counted_separately(self):
        """两栋楼各记各的。共用计数会让一栋楼的空轮次替另一栋做确认。"""
        need = _zero_rounds_to_confirm()
        for _ in range(need):
            _confirm_empty_result("ourdomain:diemen", 0)
        assert _confirm_empty_result("ourdomain:south-east", 0) is False

    def test_sources_are_counted_separately(self):
        """OurCampus 继承这套实现，两个 source 不能串。"""
        need = _zero_rounds_to_confirm()
        for _ in range(need):
            _confirm_empty_result("ourdomain:diemen", 0)
        assert _confirm_empty_result("ourcampus:diemen", 0) is False


class TestThresholdConfig:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("OURDOMAIN_ZERO_ROUNDS_TO_CONFIRM", raising=False)
        assert _zero_rounds_to_confirm() == _DEFAULT_ZERO_ROUNDS_TO_CONFIRM

    @pytest.mark.parametrize("raw,expect", [("1", 1), ("5", 5), ("999", 20), ("0", 1)])
    def test_clamped(self, monkeypatch, raw, expect):
        monkeypatch.setenv("OURDOMAIN_ZERO_ROUNDS_TO_CONFIRM", raw)
        assert _zero_rounds_to_confirm() == expect

    def test_garbage_falls_back_to_default(self, monkeypatch):
        """配错值不该让确认闸失效——那会静默恢复成「单轮就信」。"""
        monkeypatch.setenv("OURDOMAIN_ZERO_ROUNDS_TO_CONFIRM", "很多")
        assert _zero_rounds_to_confirm() == _DEFAULT_ZERO_ROUNDS_TO_CONFIRM


class TestScrapeIntegration:
    """走完整 scrape()：空结果要真的把 complete 压下去。"""

    def _task(self):
        return ScrapeTask(source="ourdomain", city_key="diemen",
                          city_display="Amsterdam Diemen")

    def _scraper(self, monkeypatch, units: dict):
        from scrapers.ourdomain import OurDomainScraper

        s = OurDomainScraper()
        monkeypatch.setattr(
            s, "_scrape_once",
            lambda **kw: (units, True, {}),
        )
        return s

    def test_empty_scrape_reports_incomplete(self, monkeypatch):
        s = self._scraper(monkeypatch, {})
        assert s.scrape(self._task()).complete is False

    def test_empty_scrape_reports_complete_after_enough_rounds(self, monkeypatch):
        s = self._scraper(monkeypatch, {})
        task = self._task()
        for _ in range(_zero_rounds_to_confirm() - 1):
            s.scrape(task)
        assert s.scrape(task).complete is True

    def test_non_empty_scrape_is_complete_immediately(self, monkeypatch):
        units = {"1": {"unit_id": "1", "apt": "#1", "fp_ids": [], "status": "Available to book"}}
        s = self._scraper(monkeypatch, units)
        r = s.scrape(self._task())
        assert r.complete is True and len(r.listings) == 1

    def test_an_already_incomplete_round_stays_incomplete(self, monkeypatch):
        """本来就没扫全的轮次不该被这道闸「确认」成完整。"""
        from scrapers.ourdomain import OurDomainScraper

        s = OurDomainScraper()
        monkeypatch.setattr(s, "_scrape_once", lambda **kw: ({}, False, {}))
        for _ in range(_zero_rounds_to_confirm() + 2):
            assert s.scrape(self._task()).complete is False
