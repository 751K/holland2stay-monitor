from __future__ import annotations

import pytest

import scrapers
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


# ── Session 复用回归（P0 把城市循环提到 dispatcher 后曾退化为 per-city Session）──

def test_holland2stay_reuses_one_session_for_all_cities(monkeypatch):
    """H2S 多城市批量抓取应该只建 1 个 Session（1 次握手 + 1 个 TLS 指纹）。

    回归保护：P0 重构曾导致每个城市新建一个 Session（N 次握手 + N 个不同
    指纹，后者是 Cloudflare 的 bot 信号）。batch_session() 应把 Session 提升
    到批次级。
    """
    import scrapers.holland2stay as h2s

    created_sessions = []

    class _FakeSession:
        def __init__(self, *a, **kw):
            created_sessions.append(self)
            self.impersonate = kw.get("impersonate")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    impersonate_calls = {"n": 0}

    def _fake_impersonate():
        impersonate_calls["n"] += 1
        return f"chrome-fp-{impersonate_calls['n']}"

    # _scrape_city_pages 不触网，返回空结果
    def _fake_scrape_city_pages(session, city_display, **kw):
        # 断言：传进来的就是那个共享 Session
        assert session in created_sessions
        return [], True

    monkeypatch.setattr(h2s.req, "Session", _FakeSession)
    monkeypatch.setattr(h2s, "get_impersonate", _fake_impersonate)
    monkeypatch.setattr(h2s, "get_proxy_url", lambda: "")
    import scraper as _scraper_mod
    monkeypatch.setattr(_scraper_mod, "_scrape_city_pages", _fake_scrape_city_pages)

    tasks = [
        ScrapeTask(source="holland2stay", city_key=str(i), city_display=f"City{i}")
        for i in range(5)
    ]
    scrapers.dispatch_scrape_tasks(tasks)

    # 5 个城市 → 只建 1 个 Session、只取 1 个指纹
    assert len(created_sessions) == 1, f"应只建 1 个 Session，实际 {len(created_sessions)}"
    assert impersonate_calls["n"] == 1, f"应只取 1 个 TLS 指纹，实际 {impersonate_calls['n']}"


def test_holland2stay_standalone_scrape_still_self_manages_session(monkeypatch):
    """不经 dispatcher 直接 scrape() 时，应自建会话（向后兼容单测路径）。"""
    import scrapers.holland2stay as h2s

    created = []

    class _FakeSession:
        def __init__(self, *a, **kw):
            created.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(h2s.req, "Session", _FakeSession)
    monkeypatch.setattr(h2s, "get_impersonate", lambda: "chrome131")
    monkeypatch.setattr(h2s, "get_proxy_url", lambda: "")
    import scraper as _scraper_mod
    monkeypatch.setattr(_scraper_mod, "_scrape_city_pages", lambda *a, **k: ([], True))

    scraper = h2s.HollandStayScraper()
    scraper.scrape(ScrapeTask(source="holland2stay", city_key="1", city_display="Eindhoven"))

    # 独立调用：建了 1 个会话（且 scrape 后该会话已关闭）
    assert len(created) == 1
