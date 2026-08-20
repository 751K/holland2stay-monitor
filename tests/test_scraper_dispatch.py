from __future__ import annotations

import asyncio
import threading
from unittest.mock import MagicMock, patch

import pytest

import scrapers
import scrapers.holland2stay as h2s
from models import Listing
from scrapers.base import AbstractScraper, BlockedError, ScrapeResult, ScrapeTask


class _OkScraper(AbstractScraper):
    source = "ok"

    def scrape(self, task: ScrapeTask) -> ScrapeResult:
        listing = Listing(
            id=f"{task.source}-1",
            name="Ok Listing",
            status="Available",
            price_raw="€700",
            available_from="2026-06-01",
            features=[],
            url="https://example.com",
            city=task.city_display,
            source=task.source,
        )
        return ScrapeResult(task=task, listings=[listing], complete=True)


class _BlockedScraper(AbstractScraper):
    source = "blocked"

    def scrape(self, task: ScrapeTask) -> ScrapeResult:
        raise BlockedError("Cloudflare 403")


def test_dispatch_keeps_partial_success_when_one_source_blocked(monkeypatch):
    monkeypatch.setitem(scrapers.SCRAPER_REGISTRY, "ok", _OkScraper)
    monkeypatch.setitem(scrapers.SCRAPER_REGISTRY, "blocked", _BlockedScraper)
    tasks = [
        ScrapeTask(source="ok", city_key="1", city_display="Eindhoven"),
        ScrapeTask(source="blocked", city_key="2", city_display="Diemen"),
    ]

    listings, completeness = scrapers.dispatch_scrape_tasks(tasks)

    assert [l.source for l in listings] == ["ok"]
    assert completeness == {
        "ok:Eindhoven": True,
        "blocked:Diemen": False,
    }


def test_dispatch_still_raises_blocked_when_all_sources_blocked(monkeypatch):
    monkeypatch.setitem(scrapers.SCRAPER_REGISTRY, "blocked", _BlockedScraper)

    with pytest.raises(BlockedError):
        scrapers.dispatch_scrape_tasks([
            ScrapeTask(source="blocked", city_key="2", city_display="Diemen"),
        ])


def test_monitor_isolated_dispatch_runs_without_thread_asyncio_loop(monkeypatch):
    import monitor

    main_thread = threading.get_ident()

    def fake_dispatch(tasks, **kwargs):
        assert threading.get_ident() != main_thread
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        return [], {"holland2stay:Eindhoven": True}

    monkeypatch.setattr(monitor, "dispatch_scrape_tasks", fake_dispatch)

    async def run():
        return await monitor._dispatch_scrape_tasks_async(
            asyncio.get_running_loop(),
            [ScrapeTask(source="holland2stay", city_key="29", city_display="Eindhoven")],
            isolated=True,
        )

    listings, completeness = asyncio.run(run())

    assert listings == []
    assert completeness == {"holland2stay:Eindhoven": True}


def test_isolated_dispatch_reuses_one_thread_across_rounds(monkeypatch):
    """H2S 必须每轮落在同一个线程上。

    回归：以前每轮 ``with ThreadPoolExecutor(...)`` 新建又销毁线程。
    Playwright 对象绑定创建线程，线程一换浏览器就作废，于是每轮都要重建
    浏览器并完整重过一次 CF 挑战。
    """
    import monitor

    seen: list[int] = []

    def fake_dispatch(tasks, **kwargs):
        seen.append(threading.get_ident())
        return [], {}

    monkeypatch.setattr(monitor, "dispatch_scrape_tasks", fake_dispatch)

    async def run_rounds():
        loop = asyncio.get_running_loop()
        for _ in range(3):
            await monitor._dispatch_scrape_tasks_async(
                loop, [], isolated=True, browser_source="holland2stay"
            )

    asyncio.run(run_rounds())

    assert len(seen) == 3
    assert len(set(seen)) == 1, f"H2S 抓取跨轮换了线程: {seen}"
    assert seen[0] != threading.get_ident()


def test_get_scraper_reuses_instance_across_calls(monkeypatch):
    """实例跨轮复用——H2S 的浏览器挂在实例上。"""
    monkeypatch.setitem(scrapers.SCRAPER_REGISTRY, "ok", _OkScraper)

    first = scrapers.get_scraper("ok")
    second = scrapers.get_scraper("ok")

    assert first is second


def test_get_scraper_rebuilds_when_registered_class_changes(monkeypatch):
    """注册表被换掉时不能继续返回用旧类建的实例。"""
    monkeypatch.setitem(scrapers.SCRAPER_REGISTRY, "ok", _OkScraper)
    first = scrapers.get_scraper("ok")

    monkeypatch.setitem(scrapers.SCRAPER_REGISTRY, "ok", _BlockedScraper)
    second = scrapers.get_scraper("ok")

    assert second is not first
    assert isinstance(second, _BlockedScraper)


# ── Browser 复用回归 ────────────────────────────────────────────

_PATCH_SCRAPE = "scrapers.holland2stay._scrape_city_pages"


def test_holland2stay_reuses_one_browser_for_all_cities(monkeypatch):
    """H2S 多城市批量抓取应该只建 1 个 BrowserFetcher。

    回归保护：P0 重构曾导致每个城市新建一个 Session；迁移到 CloakBrowser
    后 batch_session 应确保一个浏览器实例服务于批次内所有城市。
    """
    browser_instances = []

    class _FakeBrowserFetcher:
        def __init__(self, headless=True):
            browser_instances.append(self)
            self._initialized = False

        def __enter__(self):
            self._initialized = True
            return self

        def __exit__(self, *a):
            return False

        def ensure_initialized(self):
            self._initialized = True

        def fetch_gql(self, query, variables):
            return {"data": {"products": {"items": [], "page_info": {"current_page": 1, "total_pages": 1}}}}

    monkeypatch.setattr(h2s, "BrowserFetcher", _FakeBrowserFetcher)

    with patch(_PATCH_SCRAPE, return_value=([], True)):
        tasks = [
            ScrapeTask(source="holland2stay", city_key=str(i), city_display=f"City{i}")
            for i in range(5)
        ]
        scrapers.dispatch_scrape_tasks(tasks)

    # 5 个城市 → 只建 1 个 BrowserFetcher
    assert len(browser_instances) == 1, f"应只建 1 个 BrowserFetcher，实际 {len(browser_instances)}"


def test_holland2stay_standalone_scrape_still_self_manages_browser(monkeypatch):
    """不经 dispatcher 直接 scrape() 时，应自建 BrowserFetcher 并**挂在实例上复用**。

    这里原本是 scrape() 里一条独立的 else 分支：另起一个一次性浏览器、用局部
    labels dict、用完即关。它和主路径行为并不一致——每次调用都付一整轮 CF 挑战，
    而且 attr 标签的跨轮累积在它上面是坏的（每次空表，features 里会冒出裸 ID）。
    dispatcher 路径永远走不到它，这份发散因此一直没人发现。

    现在两条路径合一，形状与 XiorScraper 相同：``self._fetcher or
    self._ensure_browser()``。所以第二次 scrape() 不该再建第二个浏览器。
    """
    browser_instances = []

    class _FakeBrowserFetcher:
        def __init__(self, headless=True):
            browser_instances.append(self)
            self._initialized = False

        def __enter__(self):
            self._initialized = True
            return self

        def __exit__(self, *a):
            return False

        def ensure_initialized(self):
            self._initialized = True

        def fetch_gql(self, query, variables):
            return {"data": {"products": {"items": [], "page_info": {"current_page": 1, "total_pages": 1}}}}

    monkeypatch.setattr(h2s, "BrowserFetcher", _FakeBrowserFetcher)

    with patch(_PATCH_SCRAPE, return_value=([], True)):
        scraper_instance = h2s.HollandStayScraper()
        task = ScrapeTask(
            source="holland2stay", city_key="1", city_display="Eindhoven",
        )
        scraper_instance.scrape(task)
        scraper_instance.scrape(task)

    # 独立调用：只建 1 个 BrowserFetcher，第二次复用
    assert len(browser_instances) == 1, (
        f"独立调用每次都新建浏览器（{len(browser_instances)} 个）"
        "——每个都是一整轮 CF 挑战"
    )
    # 标签映射挂在实例上，不是每次调用一份局部空表
    assert scraper_instance._attr_labels is not None


def test_scrape_has_no_separate_standalone_branch():
    """回归守卫：scrape() 里不该再出现「自己 with 一个 BrowserFetcher」的分支。

    那条分支是主路径的一份发散拷贝，且 dispatcher 永远走不到——正是这种
    「不可达 + 行为不同」的组合让 bug 能长期藏着。
    """
    import inspect

    src = inspect.getsource(h2s.HollandStayScraper.scrape)
    assert "with BrowserFetcher(" not in src, (
        "scrape() 又自己开了一次性浏览器，行为会和 dispatcher 路径分叉"
    )


def test_browser_backed_sources_all_run_on_the_isolated_thread(monkeypatch):
    """Xior 也必须走长存单线程，不能留在默认 executor。

    回归：`_split_h2s_tasks` 只把 holland2stay 路由到隔离线程，Xior 迁到
    浏览器传输层后仍留在默认 executor。Playwright 对象绑定创建线程，默认
    executor 的线程会漂移，最终抛
    ``greenlet.error: Cannot switch to a different thread``。
    潜伏期取决于线程池碰巧复用了哪个线程。
    """
    import monitor

    assert "holland2stay" in monitor._BROWSER_SOURCES
    assert "xior" in monitor._BROWSER_SOURCES
    # 纯 HTTP 的 source 不该被拉进浏览器线程——它没有 Playwright 对象，
    # 挤进去只会和浏览器抢那一个线程。
    assert "ourdomain" not in monitor._BROWSER_SOURCES


def test_each_browser_source_gets_its_own_thread(monkeypatch):
    """两个 Playwright sync 实例不能共存于同一线程。

    回归：把 Xior 和 H2S 路由到同一个长存线程后，第一个实例在该线程装上
    event loop，第二个 launch() 随即报
    ``Playwright Sync API inside the asyncio loop``，H2S 整轮失败。
    """
    import monitor

    seen: dict[str, set[int]] = {}

    def fake_dispatch(tasks, **kwargs):
        src = tasks[0].source if tasks else "?"
        seen.setdefault(src, set()).add(threading.get_ident())
        return [], {}

    monkeypatch.setattr(monitor, "dispatch_scrape_tasks", fake_dispatch)

    async def run():
        loop = asyncio.get_running_loop()
        for _ in range(2):                      # 两轮，确认各自线程还稳定
            for src in ("holland2stay", "xior"):
                await monitor._dispatch_scrape_tasks_async(
                    loop,
                    [ScrapeTask(source=src, city_key="1", city_display="C")],
                    isolated=True,
                    browser_source=src,
                )

    asyncio.run(run())

    assert set(seen) == {"holland2stay", "xior"}
    # 每个 source 跨轮固定在同一条线程上
    assert all(len(v) == 1 for v in seen.values()), seen
    # 但两个 source 不能是同一条
    assert seen["holland2stay"] != seen["xior"], seen


class TestNoUnwiredScraperHooks:
    """``AbstractScraper`` 上不留没人调的扩展点。

    基类曾挂着 ``prewarm_session()`` 与 ``try_book(listing)`` 两个 no-op 钩子，
    设想是「支持自动预订的平台各自实现」。实际从未接线：预登录走
    ``mcore/prewarm.py → booker.create_prewarmed_session``，下单走 ``monitor``
    直接调 ``booker`` / ``bookers/*``，全仓库没有一处调过它们。

    而 H2S 那个 override 里还留着「暂未适配新 API」——在 booker 换成 NextAuth
    （v1.16.9）之后就是错的，等于在基类文档上给后来人指一条不存在的路。
    """

    def test_hooks_are_gone_from_the_base_class(self):
        from scrapers.base import AbstractScraper

        for name in ("prewarm_session", "try_book"):
            assert not hasattr(AbstractScraper, name), (
                f"AbstractScraper.{name} 又回来了——没有任何调用者，"
                "留着只会让人以为抓取层管预订"
            )

    def test_no_scraper_implements_them_either(self):
        import scrapers

        for source, cls in scrapers.SCRAPER_REGISTRY.items():
            for name in ("prewarm_session", "try_book"):
                assert not hasattr(cls, name), f"{source} 还实现着 {name}"

    def test_compat_shim_module_is_gone(self):
        """顶层 ``scraper.py`` 只剩 re-export，生产代码零 import，已删除。"""
        import importlib.util
        import pathlib

        assert not pathlib.Path("scraper.py").exists(), (
            "scraper.py 又回来了——它只是 scrapers.base 的 re-export 垫片"
        )
        assert importlib.util.find_spec("scrapers.base") is not None
