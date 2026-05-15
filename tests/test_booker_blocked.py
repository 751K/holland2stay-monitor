"""
M-3 修复：booker.py 区分 Cloudflare 403 → phase="blocked"

旧行为：booker.py 任何异常都落进 try_book 的 except Exception 通用路径，
phase 默认为 "unknown_error"，导致：
1. 日志看不出是 Cloudflare 拦的（只是普通 HTTPError）
2. _book_with_fallback 仍然继续尝试备选（每个 candidate 都 403，浪费时间）
3. monitor.run_once 给每个 candidate 发一条 booking_failed 通知（刷屏）

新行为契约（本文件验证）：
1. booker._gql 检测 403 → BookingBlockedError（区分 Cloudflare WAF / 其他 403）
2. booker.add_to_cart 直接 session.post 也走同样检测
3. try_book 捕获 BookingBlockedError → BookingResult(phase="blocked")
4. mcore.book_with_fallback 看到 phase="blocked" → 不重试备选
5. monitor.run_once 聚合 blocked 用户 → 节流通知 + 失效 prewarm 缓存
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import monitor
from monitor import run_once
from booker import (
    BookingBlockedError, BookingResult, PrewarmedSession,
    _check_blocked, _gql, try_book,
)
from mcore.booking import book_with_fallback
from notifier import BaseNotifier
from users import UserConfig
from config import AutoBookConfig, Config, CityFilter, AvailabilityFilter
from models import Listing
from storage import Storage


# Cloudflare 挑战页样本（生产真实样本）
_CLOUDFLARE_HTML = (
    '<!DOCTYPE html>\n'
    '<!--[if lt IE 7]> <html class="no-js ie6 oldie" lang="en-US"> <![endif]-->\n'
    '<html class="no-js" lang="en-US">\n'
)


def _fake_response(status_code: int, body: str = ""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = body
    resp.ok = 200 <= status_code < 300
    resp.json = MagicMock(return_value={"data": {}})
    def _raise():
        if not resp.ok:
            raise Exception(f"HTTP Error {status_code}")
    resp.raise_for_status = _raise
    return resp


def _make_listing(idx: int) -> Listing:
    return Listing(
        id=f"L-{idx}", name=f"Test-{idx}",
        status="Available to book", price_raw="€700",
        available_from="2030-01-01", features=[],
        url=f"https://t/{idx}", city="E",
        sku=f"SKU-{idx}", contract_id=42, contract_start_date="2030-01-01",
    )


# ─── _check_blocked 单元测试 ────────────────────────────────────


class TestCheckBlocked:
    def test_non_403_is_noop(self):
        # 200 / 429 / 500 都不应该抛
        for sc in (200, 401, 429, 500):
            _check_blocked(_fake_response(sc), "test")  # 不抛

    def test_403_with_cloudflare_html(self):
        with pytest.raises(BookingBlockedError) as exc:
            _check_blocked(_fake_response(403, _CLOUDFLARE_HTML), "GraphQL")
        msg = str(exc.value)
        assert "Cloudflare WAF" in msg
        assert "GraphQL" in msg
        assert "HTTPS_PROXY" in msg  # 给出可操作建议

    def test_403_with_other_html(self):
        with pytest.raises(BookingBlockedError) as exc:
            _check_blocked(_fake_response(403, '{"error":"forbidden"}'), "addNewBooking")
        msg = str(exc.value)
        assert "API 拒绝服务" in msg
        assert "addNewBooking" in msg


# ─── _gql 中的 403 检测 ────────────────────────────────────────


class TestGqlBlockedDetection:
    def test_gql_403_raises_blocked(self):
        session = MagicMock()
        session.post.return_value = _fake_response(403, _CLOUDFLARE_HTML)
        with pytest.raises(BookingBlockedError):
            _gql(session, "mutation { generateCustomerToken(...) { token } }")
        # 不应 retry，只调用一次
        assert session.post.call_count == 1

    def test_gql_200_normal(self):
        session = MagicMock()
        ok = _fake_response(200, "")
        ok.json = MagicMock(return_value={"data": {"foo": "bar"}})
        session.post.return_value = ok
        data = _gql(session, "query {}")
        assert data == {"foo": "bar"}


# ─── try_book 整体行为 ────────────────────────────────────────


class TestTryBookBlocked:
    """模拟 try_book 内部所有 GraphQL 调用都 403。"""

    def test_try_book_login_blocked_returns_phase_blocked(self):
        listing = _make_listing(1)
        session = MagicMock()
        session.post.return_value = _fake_response(403, _CLOUDFLARE_HTML)

        # mock 掉 curl_cffi Session 创建，让 try_book 直接用我们的 fake session
        with patch("booker.req.Session", return_value=session):
            result = try_book(
                listing, email="x@x.com", password="pw",
                dry_run=False,
            )

        assert result.success is False
        assert result.phase == "blocked"
        assert "Cloudflare" in result.message or "403" in result.message

    def test_try_book_with_prewarmed_still_detects_block(self):
        """已有 prewarmed session 但 H2S 又 403 了（IP 之后被屏蔽）。"""
        listing = _make_listing(1)
        sess = MagicMock()
        sess.post.return_value = _fake_response(403, _CLOUDFLARE_HTML)
        sess.close = MagicMock()
        prewarmed = PrewarmedSession(
            session=sess, token="tok",
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
    """关键：blocked 一次后 fallback 不再尝试备选房源。"""

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

        with patch("mcore.booking.try_book", side_effect=fake_try):
            result = book_with_fallback(listings, user, deadline=float("inf"))

        # 只调用第一次 try_book，不应该 fallback 到 listing 2/3
        assert call_count[0] == 1
        assert result.phase == "blocked"

    def test_race_lost_still_retries(self):
        """回归保护：race_lost 仍然走 fallback（之前的行为不变）。"""
        listings = [_make_listing(1), _make_listing(2), _make_listing(3)]
        user = UserConfig(
            name="A", id="aaaa",
            auto_book=AutoBookConfig(enabled=True, email="x@x.com", password="pw"),
        )
        call_count = [0]
        def fake_try(listing, *a, **k):
            call_count[0] += 1
            # 前两次 race_lost，第三次 success
            if call_count[0] < 3:
                return BookingResult(listing, False, "raced", phase="race_lost")
            return BookingResult(listing, True, "ok", pay_url="x", phase="success")

        with patch("mcore.booking.try_book", side_effect=fake_try):
            result = book_with_fallback(listings, user, deadline=float("inf"))

        assert call_count[0] == 3
        assert result.phase == "success"


# ─── monitor.run_once 聚合 + 节流 + 缓存失效 ──────────────────


class _CapturingNotifier(BaseNotifier):
    """记录所有 send_* 调用，便于断言通知次数。"""
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
            # prewarm 跳过：mcore.prewarm.create_prewarmed_session 返回 None
            with patch("monitor.scrape_all", side_effect=scrape_fn), \
                 patch("mcore.booking.try_book", side_effect=try_book_fn), \
                 patch("mcore.prewarm.create_prewarmed_session",
                       side_effect=lambda e, p: None):
                await run_once(cfg, storage, notifs, dry_run=False)
        asyncio.run(go())

    def test_blocked_does_not_send_per_candidate_booking_failed(self, tmp_path):
        cfg, notifs, storage, notifier = _make_run_once_setup(tmp_path)
        try:
            scrape = lambda *a, **k: [_make_listing(1), _make_listing(2)]
            blocked = lambda l, *a, **k: BookingResult(
                l, False, "CF blocked", phase="blocked"
            )
            self._run(cfg, storage, notifs, scrape, blocked)
        finally:
            storage.close()

        # 关键断言：不应发任何 booking_failed
        assert notifier.booking_failed == [], (
            f"blocked 不应触发 per-candidate booking_failed: {notifier.booking_failed}"
        )
        # 应该发了 1 条聚合 error 通知
        assert len(notifier.errors) == 1, (
            f"应聚合发 1 条 error 通知，实际 {len(notifier.errors)}"
        )
        assert "屏蔽" in notifier.errors[0] or "403" in notifier.errors[0]

    def test_blocked_notification_throttled(self, tmp_path):
        """连续 3 轮屏蔽，只发 1 条通知（30 分钟节流）。"""
        cfg, notifs, storage, notifier = _make_run_once_setup(tmp_path)
        try:
            blocked = lambda l, *a, **k: BookingResult(
                l, False, "CF blocked", phase="blocked"
            )
            # 用唯一 id 让 diff 每轮都产出 candidate
            for i in range(3):
                scrape = (lambda idx=i: lambda *a, **k: [_make_listing(idx)])()
                self._run(cfg, storage, notifs, scrape, blocked)
        finally:
            storage.close()

        # 节流：3 轮只发 1 条
        assert len(notifier.errors) == 1, (
            f"30 min 内 3 轮屏蔽应只发 1 条通知，实际 {len(notifier.errors)}"
        )

    def test_blocked_invalidates_prewarm_cache(self, tmp_path):
        """phase='blocked' 时 prewarm 缓存应失效（session 已被 CF 标记）。"""
        from monitor import prewarm_cache

        cfg, notifs, storage, notifier = _make_run_once_setup(tmp_path)
        # 手工注入一个 prewarmed
        fake_session = MagicMock()
        fake_session.closed = False
        def close_impl(): fake_session.closed = True
        fake_session.close = MagicMock(side_effect=close_impl)
        ps = PrewarmedSession(
            session=fake_session, token="tok",
            created_at=time.monotonic(),
            token_expiry=time.monotonic() + 3300,
            email="x@x.com",
        )
        prewarm_cache.set("aaaa", ps)

        try:
            scrape = lambda *a, **k: [_make_listing(1)]
            blocked = lambda l, *a, **k: BookingResult(
                l, False, "CF blocked", phase="blocked"
            )
            self._run(cfg, storage, notifs, scrape, blocked)
        finally:
            storage.close()

        # 缓存应被失效（session 已 close）
        assert "aaaa" not in prewarm_cache, "blocked 后应失效 prewarm 缓存"
        assert fake_session.closed is True

    def test_race_lost_still_sends_booking_failed(self, tmp_path):
        """回归保护：race_lost 路径不变（仍发 booking_failed）。"""
        cfg, notifs, storage, notifier = _make_run_once_setup(tmp_path)
        try:
            scrape = lambda *a, **k: [_make_listing(1)]
            race = lambda l, *a, **k: BookingResult(
                l, False, "raced", phase="race_lost"
            )
            self._run(cfg, storage, notifs, scrape, race)
        finally:
            storage.close()

        assert len(notifier.booking_failed) == 1
        assert "raced" in notifier.booking_failed[0][1]
