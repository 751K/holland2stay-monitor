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
