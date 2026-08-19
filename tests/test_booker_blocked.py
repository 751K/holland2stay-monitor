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
from booker import BookingResult, PrewarmedSession, try_book
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
            # 登录改走 fetch_plain（NextAuth）——CF 屏蔽经 ensure_initialized
            # 冒到这里。fetch_gql 也一并设上，覆盖有 prewarmed 时的下单路径。
            mock_fetcher.fetch_plain.side_effect = BlockedError("Cloudflare WAF 屏蔽（HTTP 403）")
            mock_fetcher.fetch_gql.side_effect = BlockedError("Cloudflare WAF 屏蔽（HTTP 403）")

            result = try_book(listing, email="x@x.com", password="pw", dry_run=False)

        assert result.success is False
        assert result.phase == "blocked"
        assert "403" in result.message or "Blocked" in result.message

    def test_try_book_with_prewarmed_still_detects_block(self):
        listing = _make_listing(1)
        mock_fetcher = MagicMock()
        # 占房现在走 /api/booking（fetch_encrypted_json），不再是 fetch_gql
        mock_fetcher.fetch_encrypted_json.side_effect = BlockedError("CF blocked")
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

    def test_blocked_notifies_once_not_per_candidate(self, tmp_path):
        """两个候选只发一条——按候选逐条发就成了刷屏。

        用户该收到这条：他开了自动预订，结果没订上，得知道要手动补。但发的是
        带房源的 booking_failed，不是给运维看的聚合文案——那份里有技术细节，
        还带着「影响用户: A, B, C」，会把别人的名字抄送给每一个人。
        """
        cfg, notifs, storage, notifier = _make_run_once_setup(tmp_path)
        try:
            scrape = lambda *a, **k: [_make_listing(1), _make_listing(2)]
            blocked = lambda l, *a, **k: BookingResult(l, False, "CF blocked", phase="blocked")
            self._run(cfg, storage, notifs, scrape, blocked)
        finally:
            storage.close()

        assert len(notifier.booking_failed) == 1, (
            f"两个候选应只发 1 条，实际 {len(notifier.booking_failed)}"
        )
        assert notifier.errors == [], (
            f"运维用的聚合文案发到用户渠道了: {notifier.errors}"
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


# ─── prewarm 上抛的 403 必须推进登录抑制窗口 ──────────────────

class TestPrewarmBlockSuppressesLogin:
    """回归：这条路曾经**从未执行过**。

    `mcore.prewarm.PrewarmCache.create()` 上抛的一直是裸 `BlockedError`，
    而 monitor 那三处写的是 `except BookingBlockedError` —— 一个全仓库没有任何
    地方 raise 的类。于是异常每次都落进后面的 `except Exception: ps = None`，
    CF 屏蔽被静默降级成「这次没建成，回退正常登录」，
    `_mark_h2s_login_blocked()` 一次都没被调用过。

    后果不是少一行日志：抑制窗口从来没打开过，被 CF 挡住时下一轮照样再去撞一次
    登录接口——而每次撞都是一轮完整的 CF 挑战。
    """

    def setup_method(self):
        monitor.prewarm_cache.clear()
        monitor._h2s_login_blocked_until = 0.0

    def teardown_method(self):
        monitor.prewarm_cache.clear()
        monitor._h2s_login_blocked_until = 0.0

    def _run_with_prewarm_error(self, cfg, storage, notifs, exc):
        async def go():
            def _boom(email, password):
                raise exc

            with patch("monitor.dispatch_scrape_tasks",
                       side_effect=lambda *a, **k: [_make_listing(1)]), \
                 patch("bookers.holland2stay.try_book",
                       side_effect=lambda l, *a, **k: BookingResult(
                           l, False, "n/a", phase="unknown_error")), \
                 patch("mcore.prewarm.create_prewarmed_session", side_effect=_boom):
                await run_once(cfg, storage, notifs, dry_run=False)
        asyncio.run(go())

    def test_cloudflare_block_opens_the_suppression_window(self, tmp_path):
        cfg, notifs, storage, _ = _make_run_once_setup(tmp_path)
        try:
            self._run_with_prewarm_error(
                cfg, storage, notifs, BlockedError("Cloudflare WAF 屏蔽（HTTP 403）"),
            )
        finally:
            storage.close()

        assert monitor._h2s_login_suppressed_remaining() > 0, (
            "prewarm 遇 CF 403 却没打开登录抑制窗口——"
            "这正是 BookingBlockedError 那个死类造成的静默降级"
        )

    def test_operation_rejection_does_not_open_the_window(self, tmp_path):
        """反向守卫：operation 未放行不是 CF 屏蔽，抑制多久都不会好。

        没有这条，把上面的 handler 写成 `except Exception` 也能全绿——
        然后每次预订失败都白关一小时登录链路。
        """
        from scrapers.base import OperationNotAllowedError

        cfg, notifs, storage, _ = _make_run_once_setup(tmp_path)
        try:
            self._run_with_prewarm_error(
                cfg, storage, notifs,
                OperationNotAllowedError("operation generateCustomerToken 被拒"),
            )
        finally:
            storage.close()

        assert monitor._h2s_login_suppressed_remaining() == 0, (
            "operation 未放行不该暂停登录链路：登录链路本身是好的"
        )

    def test_ordinary_failure_does_not_open_the_window(self, tmp_path):
        """普通失败（超时、解析炸了）照旧只是「这次没建成」。"""
        cfg, notifs, storage, _ = _make_run_once_setup(tmp_path)
        try:
            self._run_with_prewarm_error(
                cfg, storage, notifs, RuntimeError("socket 超时"),
            )
        finally:
            storage.close()

        assert monitor._h2s_login_suppressed_remaining() == 0


class TestNoDeadExceptionClass:
    """`BookingBlockedError` 已删除。别再引进一个没人 raise 的异常类。

    它的危害不是「多了个没用的名字」，而是让 `except` 分支看起来有覆盖、
    实际永远不执行——静默失效，测试和日志都看不出来。
    """

    def test_booker_no_longer_exports_it(self):
        import booker
        assert not hasattr(booker, "BookingBlockedError")

    def test_monitor_handlers_reference_a_live_exception(self):
        """monitor 里 `_mark_h2s_login_blocked` 所在的 except 必须捕获
        一个真的会被抛出来的类型。"""
        import ast
        import pathlib

        src = pathlib.Path("monitor.py").read_text(encoding="utf-8")
        tree = ast.parse(src)

        caught: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            calls = [
                n for n in ast.walk(node)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id == "_mark_h2s_login_blocked"
            ]
            if not calls or node.type is None:
                continue
            names = (
                [node.type] if isinstance(node.type, ast.Name)
                else list(getattr(node.type, "elts", []))
            )
            caught.update(n.id for n in names if isinstance(n, ast.Name))

        assert caught, "找不到调用 _mark_h2s_login_blocked 的 except 分支"
        assert caught == {"BlockedError"}, (
            f"这些 except 捕获了 {sorted(caught)}。prewarm 上抛的是裸 "
            f"BlockedError；捕获别的类型 = 分支永远不执行"
        )
