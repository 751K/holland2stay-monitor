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

    def fake_dispatch(tasks):
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

    def fake_dispatch(tasks):
        seen.append(threading.get_ident())
        return [], {}

    monkeypatch.setattr(monitor, "dispatch_scrape_tasks", fake_dispatch)

    async def run_rounds():
        loop = asyncio.get_running_loop()
        for _ in range(3):
            await monitor._dispatch_scrape_tasks_async(loop, [], isolated=True)

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
    monkeypatch.setattr(h2s, "_fetch_attr_labels", lambda fetcher: {})

    with patch(_PATCH_SCRAPE, return_value=([], True)):
        tasks = [
            ScrapeTask(source="holland2stay", city_key=str(i), city_display=f"City{i}")
            for i in range(5)
        ]
        scrapers.dispatch_scrape_tasks(tasks)

    # 5 个城市 → 只建 1 个 BrowserFetcher
    assert len(browser_instances) == 1, f"应只建 1 个 BrowserFetcher，实际 {len(browser_instances)}"


def test_holland2stay_standalone_scrape_still_self_manages_browser(monkeypatch):
    """不经 dispatcher 直接 scrape() 时，应自建 BrowserFetcher（单测路径）。"""
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
    monkeypatch.setattr(h2s, "_fetch_attr_labels", lambda fetcher: {})

    with patch(_PATCH_SCRAPE, return_value=([], True)):
        scraper_instance = h2s.HollandStayScraper()
        scraper_instance.scrape(ScrapeTask(
            source="holland2stay", city_key="1", city_display="Eindhoven",
        ))

    # 独立调用：建了 1 个 BrowserFetcher
    assert len(browser_instances) == 1


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
