"""403 后丢弃浏览器会话的测试。

背景
----
两个浏览器型 scraper 的 ``batch_session()`` 里都写过：

    try:
        self._ensure_browser()
        yield
    except (BlockedError, UpstreamMaintenanceError):
        self._close_browser()
        raise

但 dispatcher 是**按 task 隔离**的，``scrape()`` 抛的异常全部在那圈 per-task
``try`` 里被吃掉，根本到不了 ``yield``——这段 ``except`` 是死代码，
``HollandStayScraper`` 文档里写的「仅在 BlockedError（CF 会话过期）或进程退出
时关闭重建」实际从未发生过。

后果：H2S 抓取 403 → source 熔断 30 分钟 → canary **复用同一个被烧的浏览器**
（``_BROWSER_MAX_AGE`` 2 小时内不重建）→ 大概率再 403 → 熔断翻倍，最长 6 小时。
Xior 更直接：它靠重建浏览器来换出口 IP（``rotating_proxy=True``），不重建就
一直卡在同一个被限流的 IP 上。

现在改由 dispatcher 负责：批次里出现过 403，批次**结束后**调一次
``invalidate_session()``。
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
    UpstreamMaintenanceError,
)


class _RecordingScraper(AbstractScraper):
    """记录 invalidate_session 调用时机的探针。"""

    source = "probe"
    raises: Exception | None = None

    def __init__(self) -> None:
        self.scraped: list[str] = []
        self.invalidated_after: list[str] = []

    def scrape(self, task: ScrapeTask) -> ScrapeResult:
        self.scraped.append(task.city_display)
        if type(self).raises is not None:
            raise type(self).raises
        return ScrapeResult(task=task, listings=[], complete=True)

    def invalidate_session(self) -> None:
        # 记下「失效发生时已经抓过哪些城市」，用来断言时机
        self.invalidated_after.append(",".join(self.scraped))


def _tasks(n: int) -> list[ScrapeTask]:
    return [
        ScrapeTask(source="probe", city_key=str(i), city_display=f"City{i}")
        for i in range(n)
    ]


@pytest.fixture
def probe(monkeypatch):
    monkeypatch.setitem(scrapers.SCRAPER_REGISTRY, "probe", _RecordingScraper)
    scrapers.reset_scraper_instances()
    _RecordingScraper.raises = None
    yield
    _RecordingScraper.raises = None
    scrapers.reset_scraper_instances()


class TestBlockedInvalidatesSession:
    def test_403_drops_session(self, probe):
        _RecordingScraper.raises = BlockedError("Cloudflare 403")
        with pytest.raises(BlockedError):
            scrapers.dispatch_scrape_tasks(_tasks(1))
        scraper = scrapers.get_scraper("probe")
        assert scraper.invalidated_after == ["City0"], (
            "403 之后必须丢弃会话，否则熔断 canary 会复用被烧的浏览器"
        )

    def test_403_drops_session_after_batch_not_mid_batch(self, probe):
        """时机很重要：批次中间丢会话，同 source 后续 task 会各自重建浏览器。

        每次重建都是一轮完整 CF 挑战（H2S 上实测最长 90s+25s，失败还会连锁
        重试 3 次），一栋楼的 403 能把整批拖成分钟级。
        """
        _RecordingScraper.raises = BlockedError("Cloudflare 403")
        with pytest.raises(BlockedError):
            scrapers.dispatch_scrape_tasks(_tasks(3))
        scraper = scrapers.get_scraper("probe")
        # 三个城市全跑完才失效一次，而不是每个城市失效一次
        assert scraper.scraped == ["City0", "City1", "City2"]
        assert scraper.invalidated_after == ["City0,City1,City2"]

    def test_429_keeps_session(self, probe):
        """429 是「等等就好」，重建只是白白多过一次 CF 挑战。"""
        _RecordingScraper.raises = RateLimitError("429")
        with pytest.raises(RateLimitError):
            scrapers.dispatch_scrape_tasks(_tasks(1))
        assert scrapers.get_scraper("probe").invalidated_after == []

    def test_maintenance_keeps_session(self, probe):
        """平台维护是对方的事，浏览器没坏——丢了只是下轮多一次冷启动。"""
        _RecordingScraper.raises = UpstreamMaintenanceError("维护中")
        with pytest.raises(UpstreamMaintenanceError):
            scrapers.dispatch_scrape_tasks(_tasks(1))
        assert scrapers.get_scraper("probe").invalidated_after == []

    def test_success_keeps_session(self, probe):
        """正常轮次绝不能丢——浏览器跨轮复用是省掉每轮 CF 挑战的前提。"""
        scrapers.dispatch_scrape_tasks(_tasks(2))
        assert scrapers.get_scraper("probe").invalidated_after == []

    def test_partial_403_still_drops_session(self, probe):
        """一栋楼 403、其余成功：会话仍然可疑，照丢。"""
        class _FlakyScraper(_RecordingScraper):
            source = "probe"

            def scrape(self, task):
                self.scraped.append(task.city_display)
                if task.city_display == "City1":
                    raise BlockedError("Cloudflare 403")
                return ScrapeResult(task=task, listings=[], complete=True)

        scrapers.SCRAPER_REGISTRY["probe"] = _FlakyScraper
        scrapers.reset_scraper_instances()

        listings, completeness = scrapers.dispatch_scrape_tasks(_tasks(3))

        scraper = scrapers.get_scraper("probe")
        assert completeness == {"City0": True, "City1": False, "City2": True}
        assert scraper.invalidated_after == ["City0,City1,City2"]


class TestBatchSessionHasNoDeadHandler:
    """两个 scraper 的 batch_session 不该再自己处理 BlockedError。

    留着它有害：读代码的人会以为「403 → 关浏览器」这条路还在，实际 dispatcher
    的 per-task try 早就把异常吃掉了，那段 except 永远不会执行。
    """

    @pytest.mark.parametrize("module_name, cls_name", [
        ("scrapers.holland2stay", "HollandStayScraper"),
        ("scrapers.xior", "XiorScraper"),
    ])
    def test_batch_session_leaves_session_handling_to_dispatcher(
        self, module_name, cls_name, monkeypatch,
    ):
        import importlib

        mod = importlib.import_module(module_name)
        scraper = getattr(mod, cls_name)()

        closed: list[bool] = []
        monkeypatch.setattr(scraper, "_ensure_browser", lambda: None)
        monkeypatch.setattr(scraper, "_close_browser", lambda: closed.append(True))

        # 直接把 BlockedError 扔进批次上下文——生产里 dispatcher 不会这么做
        # （per-task try 先吃掉了），这里是为了证明「即使扔进去也不自己关」。
        cm = scraper.batch_session()
        cm.__enter__()
        suppressed = cm.__exit__(BlockedError, BlockedError("403"), None)

        assert suppressed is False, "batch_session 不该吞掉 403"
        assert closed == [], (
            f"{cls_name}.batch_session 又开始自己关浏览器了；"
            "会话失效现在由 dispatcher 在批次结束后统一负责"
        )


# ── 429 的批次级短路（原 test_scraper_rate_limit_shortcircuit.py） ──
#
# 背景
# ----
# dispatcher 原本把 ``RateLimitError`` 和 ``BlockedError`` 塞进同一个 ``except``，
# 但只给 403 做了 source 级处理（``source_blocked`` → 批次末尾丢会话），429 走完
# ``hard_failures.append`` 就 ``continue`` 到下一个城市。
#
# 问题是 **429 是服务端对本客户端出口 IP 的配额判定**，不是对某一栋楼的。第一个
# task 把退避跑满仍然 429，剩下的 task 必然也 429——区别只在于每个都要先睡满一
# 整轮退避去把这件已知的事重新证明一遍。
#
# 2026-08-20 生产日志实测（Xior，4 个城市）::
#
#     16:21:06 [ERROR] xior:Amsterdam Karspeldreef  ... 429
#     16:22:37 [ERROR] xior:Amsterdam Naritaweg     ... 429   ← +91s
#     16:24:08 [ERROR] xior:Eindhoven Kronehoefstraat ... 429 ← +91s
#     16:25:39 [ERROR] xior:Eindhoven Zernikestraat ... 429   ← +91s
#
# 整齐的 91 秒 = 30s + 60s 退避，4 个城市 ≈ 6 分钟。而这 6 分钟**整轮阻塞**，同轮
# 里 H2S / OurDomain 的房源一起延迟交付。这样的爆发从 08-10 起每天约 10 次。
#
# 现在：批次里出现过 429，同 source 的剩余 task 直接判失败、一个请求都不发。

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


def _rl_tasks(n: int, source: str = "probe") -> list[ScrapeTask]:
    return [
        ScrapeTask(source=source, city_key=str(i), city_display=f"City{i}")
        for i in range(n)
    ]


@pytest.fixture
def _rl_probe(monkeypatch):
    monkeypatch.setitem(scrapers.SCRAPER_REGISTRY, "probe", _CountingScraper)
    monkeypatch.setitem(scrapers.SCRAPER_REGISTRY, "other", _OtherScraper)
    scrapers.reset_scraper_instances()
    _CountingScraper.raises_on = {}
    _OtherScraper.raises_on = {}
    yield
    _CountingScraper.raises_on = {}
    _OtherScraper.raises_on = {}
    scrapers.reset_scraper_instances()


def _rl_instance(source: str = "probe"):
    return scrapers.get_scraper(source)


class TestRateLimitShortCircuitsSource:

    def test_remaining_tasks_are_never_scraped(self, _rl_probe):
        """第一个城市 429 → 后面 3 个城市一个请求都不发。"""
        _CountingScraper.raises_on = {"City0": RateLimitError("429")}

        with pytest.raises(RateLimitError):
            scrapers.dispatch_scrape_tasks(_rl_tasks(4))

        assert _rl_instance().scraped == ["City0"], (
            "429 之后仍然给剩余城市发了请求——每个都要先睡满一轮退避，"
            "这正是每天空转 1 小时的来源"
        )

    def test_short_circuit_still_lets_the_source_error_propagate(self, _rl_probe):
        """短路不能把「全 source 失败」这个信号吃掉。

        ``SourceCircuits`` 跳闸的输入就是 dispatcher 上抛的 source 级异常。
        如果短路让 success_count 或 hard_failures 任何一边失真，熔断器就永远
        不跳闸——429 会一轮接一轮地重演，正是这次要修的反面。
        """
        _CountingScraper.raises_on = {"City0": RateLimitError("429")}

        with pytest.raises(RateLimitError) as ei:
            scrapers.dispatch_scrape_tasks(_rl_tasks(4))

        assert "429" in str(ei.value)

    def test_skipped_tasks_are_marked_incomplete(self, _rl_probe):
        """跳过的城市 completeness 必须是 False——没抓就是没抓完。"""
        _CountingScraper.raises_on = {"City0": RateLimitError("429")}

        # 加一个成功的 source，避免走「全失败上抛」那条路，好拿到返回值
        _, completeness = scrapers.dispatch_scrape_tasks(
            _rl_tasks(3) + _rl_tasks(1, source="other"), multi_source=True,
        )

        for i in range(3):
            assert completeness[f"probe:City{i}"] is False
        assert completeness["other:City0"] is True

    def test_skipped_tasks_carry_the_rate_limit_error(self, _rl_probe):
        """上抛的必须是 RateLimitError 本身，不能被压成别的类型。

        ``_ROUND_FAILURE_PRIORITY`` 和各 source 的 CircuitPolicy.trips_on
        都是按异常类型分派的——类型错了，Xior 的 429 就不会触发熔断。
        """
        original = RateLimitError("Xior 返回 429 Too Many Requests")
        _CountingScraper.raises_on = {"City0": original}

        with pytest.raises(RateLimitError) as ei:
            scrapers.dispatch_scrape_tasks(_rl_tasks(4))

        assert ei.value is original


class TestShortCircuitBoundaries:

    def test_rate_limit_on_last_task_changes_nothing(self, _rl_probe):
        """429 出现在最后一个城市 → 没有可跳过的，行为与改动前一致。"""
        _CountingScraper.raises_on = {"City3": RateLimitError("429")}

        listings, completeness = scrapers.dispatch_scrape_tasks(_rl_tasks(4))

        assert _rl_instance().scraped == ["City0", "City1", "City2", "City3"]
        assert completeness["City3"] is False
        assert completeness["City0"] is True

    def test_earlier_successes_survive(self, _rl_probe):
        """City0/1 已经抓好 → 不因为 City2 的 429 而丢失。"""
        _CountingScraper.raises_on = {"City2": RateLimitError("429")}

        listings, completeness = scrapers.dispatch_scrape_tasks(_rl_tasks(4))

        assert _rl_instance().scraped == ["City0", "City1", "City2"]
        assert completeness["City0"] is True
        assert completeness["City1"] is True
        assert completeness["City2"] is False
        assert completeness["City3"] is False

    def test_short_circuit_is_per_source(self, _rl_probe):
        """probe 被限流不影响 other——429 是按出口 IP + 站点算的，
        但 dispatcher 的隔离单位是 source，别把别人一起停了。"""
        _CountingScraper.raises_on = {"City0": RateLimitError("429")}

        scrapers.dispatch_scrape_tasks(
            _rl_tasks(3) + _rl_tasks(2, source="other"), multi_source=True,
        )

        assert _rl_instance("probe").scraped == ["City0"]
        assert _rl_instance("other").scraped == ["City0", "City1"]

    def test_blocked_error_still_does_not_short_circuit(self, _rl_probe):
        """403 的处理**刻意保持不变**：继续跑完，批次末尾丢一次会话。

        403 和 429 不同——它是会话/指纹被标记，换个会话就可能好；而且现有
        注释明确写过「批次中间丢会话会让后续 task 各自触发一次浏览器重建」。
        这次只动 429，403 一个字节都不改。
        """
        _CountingScraper.raises_on = {"City0": BlockedError("403")}

        _, completeness = scrapers.dispatch_scrape_tasks(_rl_tasks(4))

        assert _rl_instance().scraped == ["City0", "City1", "City2", "City3"]
        assert completeness["City0"] is False
        assert completeness["City3"] is True
        assert _rl_instance().invalidated == 1

    def test_network_error_still_does_not_short_circuit(self, _rl_probe):
        """网络抖动是单次请求的事，不是配额判定——不短路。"""
        _CountingScraper.raises_on = {"City0": ScrapeNetworkError("boom")}

        scrapers.dispatch_scrape_tasks(_rl_tasks(4))

        assert _rl_instance().scraped == ["City0", "City1", "City2", "City3"]

    def test_rate_limit_does_not_invalidate_session(self, _rl_probe):
        """429 不丢会话——「等等就好」，重建只是白白多过一次 CF 挑战。

        这条是既有决策（见 dispatcher 里的注释），短路不该顺手改掉它。
        """
        _CountingScraper.raises_on = {"City0": RateLimitError("429")}

        with pytest.raises(RateLimitError):
            scrapers.dispatch_scrape_tasks(_rl_tasks(4))

        assert _rl_instance().invalidated == 0
