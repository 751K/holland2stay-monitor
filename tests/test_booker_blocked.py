"""
booker.py Cloudflare 403 → phase="blocked" 测试（BrowserFetcher 版）。

旧 curl_cffi _check_blocked / _gql 已删除；403 检测现在在 BrowserFetcher 内部。
本测试在 try_book / book_with_fallback / monitor run_once 级别验证 blocked 契约。
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import monitor
from monitor import run_once
from booker import BookingBlockedError, BookingResult, PrewarmedSession, try_book
from mcore.booking import book_with_fallback
from notifier import BaseNotifier
from users import UserConfig
from config import AutoBookConfig, Config, CityFilter, AvailabilityFilter
from models import Listing
from storage import Storage
from scrapers.base import BlockedError


def _make_listing(idx: int) -> Listing:
    return Listing(
        id=f"L-{idx}", name=f"Test-{idx}",
        status="Available to book", price_raw="€700",
        available_from="2030-01-01", features=[],
        url=f"https://t/{idx}", city="E",
        sku=f"SKU-{idx}", contract_id=42, contract_start_date="2030-01-01",
    )


# ─── try_book blocked 行为 ────────────────────────────────────

class TestTryBookBlocked:
    """BrowserFetcher 抛 BlockedError 时 try_book 应捕获并返回 phase='blocked'。"""

    def test_try_book_login_blocked_returns_phase_blocked(self):
        listing = _make_listing(1)

        # BrowserFetcher 在 login 时抛 BlockedError
        with patch("booker.BrowserFetcher") as MockFetcher:
            mock_fetcher = MockFetcher.return_value
            mock_fetcher.__enter__.return_value = mock_fetcher
            mock_fetcher.__exit__.return_value = False
            # ensure_initialized 正常，但 fetch_gql 抛 BlockedError
            mock_fetcher.fetch_gql.side_effect = BlockedError("Cloudflare WAF 屏蔽（HTTP 403）")

            result = try_book(listing, email="x@x.com", password="pw", dry_run=False)

        assert result.success is False
        assert result.phase == "blocked"
        assert "403" in result.message or "Blocked" in result.message

    def test_try_book_with_prewarmed_still_detects_block(self):
        listing = _make_listing(1)
        mock_fetcher = MagicMock()
        mock_fetcher.fetch_gql.side_effect = BlockedError("CF blocked")
        mock_fetcher.close = MagicMock()
        prewarmed = PrewarmedSession(
            fetcher=mock_fetcher, token="tok",
            created_at=time.monotonic(),
            token_expiry=time.monotonic() + 3300,
            email="x@x.com",
        )
        result = try_book(
            listing, email="x@x.com", password="pw",
            dry_run=False, prewarmed=prewarmed,
        )
        assert result.phase == "blocked"


# ─── book_with_fallback：blocked 时停止重试 ──────────────────

class TestBookWithFallbackBlocked:
    def test_blocked_stops_fallback(self):
        listings = [_make_listing(1), _make_listing(2), _make_listing(3)]
        user = UserConfig(
            name="A", id="aaaa",
            auto_book=AutoBookConfig(enabled=True, email="x@x.com", password="pw"),
        )
        call_count = [0]
        def fake_try(listing, *a, **k):
            call_count[0] += 1
            return BookingResult(listing, False, "CF blocked", phase="blocked")

        with patch("bookers.holland2stay.try_book", side_effect=fake_try):
            result = book_with_fallback(listings, user, deadline=float("inf"))

        assert call_count[0] == 1
        assert result.phase == "blocked"

    def test_race_lost_still_retries(self):
        listings = [_make_listing(1), _make_listing(2), _make_listing(3)]
        user = UserConfig(
            name="A", id="aaaa",
            auto_book=AutoBookConfig(enabled=True, email="x@x.com", password="pw"),
        )
        call_count = [0]
        def fake_try(listing, *a, **k):
            call_count[0] += 1
            if call_count[0] < 3:
                return BookingResult(listing, False, "raced", phase="race_lost")
            return BookingResult(listing, True, "ok", pay_url="x", phase="success")

        with patch("bookers.holland2stay.try_book", side_effect=fake_try):
            result = book_with_fallback(listings, user, deadline=float("inf"))

        assert call_count[0] == 3
        assert result.phase == "success"


# ─── monitor.run_once 聚合 + 节流 + 缓存失效 ──────────────────

class _CapturingNotifier(BaseNotifier):
    has_channels = True

    def __init__(self):
        self.errors: list[str] = []
        self.booking_failed: list[tuple] = []
        self.booking_success: list[tuple] = []

    async def _send(self, t):
        self.errors.append(t)
        return True
    async def send_error(self, msg):
        self.errors.append(msg)
        return True
    async def send_booking_failed(self, listing, msg):
        self.booking_failed.append((listing.id, msg))
        return True
    async def send_booking_success(self, listing, msg, pay_url="", contract_start_date=""):
        self.booking_success.append((listing.id, msg))
        return True
    async def send_new_listing(self, listing): return True
    async def send_status_change(self, *a, **k): return True
    async def send_heartbeat(self, *a, **k): return True
    async def close(self): pass


def _make_run_once_setup(tmp_path):
    cfg = Config(
        check_interval=300,
        cities=[CityFilter(name="E", id=29)],
        availability_filters=[AvailabilityFilter(label="A", id=179)],
        db_path=tmp_path / "test.db", log_level="WARNING",
    )
    user = UserConfig(
        name="A", id="aaaa", enabled=True, notifications_enabled=True,
        notification_channels=[],
        auto_book=AutoBookConfig(enabled=True, email="x@x.com", password="pw"),
    )
    notifier = _CapturingNotifier()
    storage = Storage(tmp_path / "test.db", timezone_str="UTC")
    return cfg, [(user, notifier)], storage, notifier


class TestMonitorRunOnceBlockedAggregation:
    def setup_method(self):
        monitor.prewarm_cache.clear()
        monitor._last_block_notify_at = 0.0

    def teardown_method(self):
        monitor.prewarm_cache.clear()
        monitor._last_block_notify_at = 0.0

    def _run(self, cfg, storage, notifs, scrape_fn, try_book_fn):
        async def go():
            with patch("monitor.dispatch_scrape_tasks", side_effect=scrape_fn), \
                 patch("bookers.holland2stay.try_book", side_effect=try_book_fn), \
                 patch("mcore.prewarm.create_prewarmed_session",
                       side_effect=lambda e, p: None):
                await run_once(cfg, storage, notifs, dry_run=False)
        asyncio.run(go())

    def test_blocked_does_not_send_per_candidate_booking_failed(self, tmp_path):
        cfg, notifs, storage, notifier = _make_run_once_setup(tmp_path)
        try:
            scrape = lambda *a, **k: [_make_listing(1), _make_listing(2)]
            blocked = lambda l, *a, **k: BookingResult(l, False, "CF blocked", phase="blocked")
            self._run(cfg, storage, notifs, scrape, blocked)
        finally:
            storage.close()

        assert notifier.booking_failed == [], (
            f"blocked 不应触发 per-candidate booking_failed: {notifier.booking_failed}"
        )
        assert len(notifier.errors) == 1, (
            f"应聚合发 1 条 error 通知，实际 {len(notifier.errors)}"
        )

    def test_blocked_invalidates_prewarm_cache(self, tmp_path):
        from monitor import prewarm_cache

        cfg, notifs, storage, notifier = _make_run_once_setup(tmp_path)
        fake_fetcher = MagicMock()
        fake_fetcher.closed = False
        def close_impl(): fake_fetcher.closed = True
        fake_fetcher.close = MagicMock(side_effect=close_impl)
        ps = PrewarmedSession(
            fetcher=fake_fetcher, token="tok",
            created_at=time.monotonic(),
            token_expiry=time.monotonic() + 3300,
            email="x@x.com",
        )
        prewarm_cache.set("aaaa", ps)

        try:
            scrape = lambda *a, **k: [_make_listing(1)]
            blocked = lambda l, *a, **k: BookingResult(l, False, "CF blocked", phase="blocked")
            self._run(cfg, storage, notifs, scrape, blocked)
        finally:
            storage.close()

        assert "aaaa" not in prewarm_cache, "blocked 后应失效 prewarm 缓存"
        assert fake_fetcher.closed is True

    def test_race_lost_still_sends_booking_failed(self, tmp_path):
        cfg, notifs, storage, notifier = _make_run_once_setup(tmp_path)
        try:
            scrape = lambda *a, **k: [_make_listing(1)]
            race = lambda l, *a, **k: BookingResult(l, False, "raced", phase="race_lost")
            self._run(cfg, storage, notifs, scrape, race)
        finally:
            storage.close()

        assert len(notifier.booking_failed) == 1
        assert "raced" in notifier.booking_failed[0][1]
