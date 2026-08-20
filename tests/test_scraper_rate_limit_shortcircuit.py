"""429 的批次级短路测试。

背景
----
dispatcher 原本把 ``RateLimitError`` 和 ``BlockedError`` 塞进同一个 ``except``，
但只给 403 做了 source 级处理（``source_blocked`` → 批次末尾丢会话），429 走完
``hard_failures.append`` 就 ``continue`` 到下一个城市。

问题是 **429 是服务端对本客户端出口 IP 的配额判定**，不是对某一栋楼的。第一个
task 把退避跑满仍然 429，剩下的 task 必然也 429——区别只在于每个都要先睡满一
整轮退避去把这件已知的事重新证明一遍。

2026-08-20 生产日志实测（Xior，4 个城市）::

    16:21:06 [ERROR] xior:Amsterdam Karspeldreef  ... 429
    16:22:37 [ERROR] xior:Amsterdam Naritaweg     ... 429   ← +91s
    16:24:08 [ERROR] xior:Eindhoven Kronehoefstraat ... 429 ← +91s
    16:25:39 [ERROR] xior:Eindhoven Zernikestraat ... 429   ← +91s

整齐的 91 秒 = 30s + 60s 退避，4 个城市 ≈ 6 分钟。而这 6 分钟**整轮阻塞**，同轮
里 H2S / OurDomain 的房源一起延迟交付。这样的爆发从 08-10 起每天约 10 次。

现在：批次里出现过 429，同 source 的剩余 task 直接判失败、一个请求都不发。
"""
from __future__ import annotations

import pytest

import scrapers
from scrapers.base import (
    AbstractScraper,
    BlockedError,
    RateLimitError,
    ScrapeNetworkError,
    ScrapeResult,
    ScrapeTask,
)


class _CountingScraper(AbstractScraper):
    """按 city_display 决定抛什么；记录实际发生过的 scrape 调用。"""

    source = "probe"
    raises_on: dict[str, Exception] = {}

    def __init__(self) -> None:
        self.scraped: list[str] = []
        self.invalidated = 0

    def scrape(self, task: ScrapeTask) -> ScrapeResult:
        self.scraped.append(task.city_display)
        exc = type(self).raises_on.get(task.city_display)
        if exc is not None:
            raise exc
        return ScrapeResult(task=task, listings=[], complete=True)

    def invalidate_session(self) -> None:
        self.invalidated += 1


class _OtherScraper(_CountingScraper):
    source = "other"
    raises_on: dict[str, Exception] = {}


def _tasks(n: int, source: str = "probe") -> list[ScrapeTask]:
    return [
        ScrapeTask(source=source, city_key=str(i), city_display=f"City{i}")
        for i in range(n)
    ]


@pytest.fixture
def probe(monkeypatch):
    monkeypatch.setitem(scrapers.SCRAPER_REGISTRY, "probe", _CountingScraper)
    monkeypatch.setitem(scrapers.SCRAPER_REGISTRY, "other", _OtherScraper)
    scrapers.reset_scraper_instances()
    _CountingScraper.raises_on = {}
    _OtherScraper.raises_on = {}
    yield
    _CountingScraper.raises_on = {}
    _OtherScraper.raises_on = {}
    scrapers.reset_scraper_instances()


def _instance(source: str = "probe"):
    return scrapers.get_scraper(source)


# ─── 核心：429 后不再发请求 ──────────────────────────────────────

class TestRateLimitShortCircuitsSource:

    def test_remaining_tasks_are_never_scraped(self, probe):
        """第一个城市 429 → 后面 3 个城市一个请求都不发。"""
        _CountingScraper.raises_on = {"City0": RateLimitError("429")}

        with pytest.raises(RateLimitError):
            scrapers.dispatch_scrape_tasks(_tasks(4))

        assert _instance().scraped == ["City0"], (
            "429 之后仍然给剩余城市发了请求——每个都要先睡满一轮退避，"
            "这正是每天空转 1 小时的来源"
        )

    def test_short_circuit_still_lets_the_source_error_propagate(self, probe):
        """短路不能把「全 source 失败」这个信号吃掉。

        ``SourceCircuits`` 跳闸的输入就是 dispatcher 上抛的 source 级异常。
        如果短路让 success_count 或 hard_failures 任何一边失真，熔断器就永远
        不跳闸——429 会一轮接一轮地重演，正是这次要修的反面。
        """
        _CountingScraper.raises_on = {"City0": RateLimitError("429")}

        with pytest.raises(RateLimitError) as ei:
            scrapers.dispatch_scrape_tasks(_tasks(4))

        assert "429" in str(ei.value)

    def test_skipped_tasks_are_marked_incomplete(self, probe):
        """跳过的城市 completeness 必须是 False——没抓就是没抓完。"""
        _CountingScraper.raises_on = {"City0": RateLimitError("429")}

        # 加一个成功的 source，避免走「全失败上抛」那条路，好拿到返回值
        _, completeness = scrapers.dispatch_scrape_tasks(
            _tasks(3) + _tasks(1, source="other"), multi_source=True,
        )

        for i in range(3):
            assert completeness[f"probe:City{i}"] is False
        assert completeness["other:City0"] is True

    def test_skipped_tasks_carry_the_rate_limit_error(self, probe):
        """上抛的必须是 RateLimitError 本身，不能被压成别的类型。

        ``_ROUND_FAILURE_PRIORITY`` 和各 source 的 CircuitPolicy.trips_on
        都是按异常类型分派的——类型错了，Xior 的 429 就不会触发熔断。
        """
        original = RateLimitError("Xior 返回 429 Too Many Requests")
        _CountingScraper.raises_on = {"City0": original}

        with pytest.raises(RateLimitError) as ei:
            scrapers.dispatch_scrape_tasks(_tasks(4))

        assert ei.value is original


# ─── 边界：不该短路的情况 ────────────────────────────────────────

class TestShortCircuitBoundaries:

    def test_rate_limit_on_last_task_changes_nothing(self, probe):
        """429 出现在最后一个城市 → 没有可跳过的，行为与改动前一致。"""
        _CountingScraper.raises_on = {"City3": RateLimitError("429")}

        listings, completeness = scrapers.dispatch_scrape_tasks(_tasks(4))

        assert _instance().scraped == ["City0", "City1", "City2", "City3"]
        assert completeness["City3"] is False
        assert completeness["City0"] is True

    def test_earlier_successes_survive(self, probe):
        """City0/1 已经抓好 → 不因为 City2 的 429 而丢失。"""
        _CountingScraper.raises_on = {"City2": RateLimitError("429")}

        listings, completeness = scrapers.dispatch_scrape_tasks(_tasks(4))

        assert _instance().scraped == ["City0", "City1", "City2"]
        assert completeness["City0"] is True
        assert completeness["City1"] is True
        assert completeness["City2"] is False
        assert completeness["City3"] is False

    def test_short_circuit_is_per_source(self, probe):
        """probe 被限流不影响 other——429 是按出口 IP + 站点算的，
        但 dispatcher 的隔离单位是 source，别把别人一起停了。"""
        _CountingScraper.raises_on = {"City0": RateLimitError("429")}

        scrapers.dispatch_scrape_tasks(
            _tasks(3) + _tasks(2, source="other"), multi_source=True,
        )

        assert _instance("probe").scraped == ["City0"]
        assert _instance("other").scraped == ["City0", "City1"]

    def test_blocked_error_still_does_not_short_circuit(self, probe):
        """403 的处理**刻意保持不变**：继续跑完，批次末尾丢一次会话。

        403 和 429 不同——它是会话/指纹被标记，换个会话就可能好；而且现有
        注释明确写过「批次中间丢会话会让后续 task 各自触发一次浏览器重建」。
        这次只动 429，403 一个字节都不改。
        """
        _CountingScraper.raises_on = {"City0": BlockedError("403")}

        _, completeness = scrapers.dispatch_scrape_tasks(_tasks(4))

        assert _instance().scraped == ["City0", "City1", "City2", "City3"]
        assert completeness["City0"] is False
        assert completeness["City3"] is True
        assert _instance().invalidated == 1

    def test_network_error_still_does_not_short_circuit(self, probe):
        """网络抖动是单次请求的事，不是配额判定——不短路。"""
        _CountingScraper.raises_on = {"City0": ScrapeNetworkError("boom")}

        scrapers.dispatch_scrape_tasks(_tasks(4))

        assert _instance().scraped == ["City0", "City1", "City2", "City3"]

    def test_rate_limit_does_not_invalidate_session(self, probe):
        """429 不丢会话——「等等就好」，重建只是白白多过一次 CF 挑战。

        这条是既有决策（见 dispatcher 里的注释），短路不该顺手改掉它。
        """
        _CountingScraper.raises_on = {"City0": RateLimitError("429")}

        with pytest.raises(RateLimitError):
            scrapers.dispatch_scrape_tasks(_tasks(4))

        assert _instance().invalidated == 0
