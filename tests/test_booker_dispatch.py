"""
bookers/__init__.py 的 dispatcher 单元测试
==========================================

覆盖三种路径：
1. listing.source="holland2stay" → 路由到 HollandStayBooker（验证转发）
2. listing.source="ourdomain"   → 未注册 → 返回 phase="unsupported"
3. listing.source="unknown_xxx" → 同上，phase="unsupported"

以及 mcore.booking.book_with_fallback 过滤候选的回归保护：
- 混合 source 列表：H2S + OurDomain 候选混着进，OurDomain 应被过滤、
  不会触发任何 dispatch（重要：避免对 OurDomain 用户产生"预订失败"误报）
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from bookers import (
    AbstractBooker,
    BOOKER_REGISTRY,
    BookingRequest,
    BookingResult,
    HollandStayBooker,
    dispatch_book,
    get_booker,
    supports_booking,
)
from booker import BookingResult as RawBookingResult
from mcore.booking import book_with_fallback
from models import Listing
from users import AutoBookConfig, UserConfig


# ──────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────


def _make_listing(lid: str, source: str = "holland2stay") -> Listing:
    return Listing(
        id=lid,
        name=f"unit {lid}",
        status="Available to book",
        price_raw="€700",
        available_from="2026-06-01",
        features=["Area: 30.0 m²"],
        url="http://example.com",
        city="Amsterdam",
        source=source,
    )


def _make_user() -> UserConfig:
    return UserConfig(
        name="alice", id="a1a1a1a1",
        auto_book=AutoBookConfig(
            enabled=True, email="alice@example.com", password="pw",
        ),
    )


# ──────────────────────────────────────────────────────────────────
# Registry / lookup
# ──────────────────────────────────────────────────────────────────


class TestRegistry:
    def test_h2s_registered(self):
        assert "holland2stay" in BOOKER_REGISTRY
        assert BOOKER_REGISTRY["holland2stay"] is HollandStayBooker

    def test_ourdomain_not_registered(self):
        assert "ourdomain" not in BOOKER_REGISTRY

    def test_get_booker_h2s_returns_instance(self):
        booker = get_booker("holland2stay")
        assert isinstance(booker, HollandStayBooker)
        assert isinstance(booker, AbstractBooker)

    def test_get_booker_unregistered_returns_none(self):
        assert get_booker("ourdomain") is None
        assert get_booker("nonsense") is None
        assert get_booker("") is None

    def test_supports_booking(self):
        assert supports_booking("holland2stay") is True
        assert supports_booking("ourdomain") is False
        assert supports_booking("") is False


# ──────────────────────────────────────────────────────────────────
# dispatch_book routing
# ──────────────────────────────────────────────────────────────────


class TestDispatchBook:
    def test_h2s_listing_calls_holland2stay_booker(self):
        listing = _make_listing("h2s-1", source="holland2stay")
        user = _make_user()
        expected = RawBookingResult(listing, success=True, message="ok",
                                    pay_url="http://pay", phase="success")
        with patch("bookers.holland2stay.try_book", return_value=expected) as mock:
            result = dispatch_book(
                BookingRequest(listing=listing, user=user, dry_run=False)
            )
        # try_book 真的被叫了，且参数是 H2S 专属字段
        mock.assert_called_once()
        args, kwargs = mock.call_args
        assert args[0] is listing
        assert kwargs["dry_run"] is False
        assert "cancel_enabled" in kwargs
        assert "payment_method" in kwargs
        # 返回值完整透传
        assert result is expected
        assert result.phase == "success"

    def test_ourdomain_listing_returns_unsupported(self):
        listing = _make_listing("od-1", source="ourdomain")
        user = _make_user()
        # 不应调用 try_book —— 完全短路
        with patch("bookers.holland2stay.try_book") as mock:
            result = dispatch_book(
                BookingRequest(listing=listing, user=user)
            )
        mock.assert_not_called()
        assert result.success is False
        assert result.phase == "unsupported"
        assert result.listing is listing
        # 消息提示用户手动申请
        assert "手动" in result.message or "manual" in result.message.lower()

    def test_unknown_source_returns_unsupported(self):
        listing = _make_listing("x-1", source="parariusxxx")
        user = _make_user()
        result = dispatch_book(BookingRequest(listing=listing, user=user))
        assert result.phase == "unsupported"
        assert result.success is False

    def test_empty_source_falls_through_unsupported(self):
        listing = _make_listing("y-1", source="")
        user = _make_user()
        result = dispatch_book(BookingRequest(listing=listing, user=user))
        # 空 source → 视为未知，phase=unsupported
        assert result.phase == "unsupported"


# ──────────────────────────────────────────────────────────────────
# book_with_fallback 的候选过滤行为
# ──────────────────────────────────────────────────────────────────


class TestBookWithFallbackFiltering:
    def test_only_ourdomain_candidates_short_circuit(self):
        """全部候选都是 OurDomain → book_with_fallback 立即返回 None，
        不调用任何 try_book（避免误报失败通知）。"""
        candidates = [
            _make_listing("od-1", source="ourdomain"),
            _make_listing("od-2", source="ourdomain"),
        ]
        user = _make_user()
        with patch("bookers.holland2stay.try_book") as mock:
            result = book_with_fallback(candidates, user, deadline=float("inf"))
        mock.assert_not_called()
        assert result is None

    def test_mixed_candidates_only_h2s_tried(self):
        """H2S + OurDomain 混合：OurDomain 全跳过，H2S 候选正常尝试。"""
        h2s_1 = _make_listing("h2s-1", source="holland2stay")
        od_1 = _make_listing("od-1", source="ourdomain")
        h2s_2 = _make_listing("h2s-2", source="holland2stay")
        candidates = [h2s_1, od_1, h2s_2]   # 中间夹一个 OD
        user = _make_user()

        call_listings = []
        def fake_try(listing, *a, **k):
            call_listings.append(listing)
            return RawBookingResult(listing, success=True, message="ok",
                                    pay_url="http://pay", phase="success")

        with patch("bookers.holland2stay.try_book", side_effect=fake_try):
            result = book_with_fallback(candidates, user, deadline=float("inf"))

        # 第一个 H2S 成功就返回——OD 永远不参与
        assert len(call_listings) == 1
        assert call_listings[0] is h2s_1
        assert result.success is True

    def test_mixed_candidates_h2s_race_lost_falls_through_to_h2s_only(self):
        """h2s_1 race_lost → 不会去试 od_1（被过滤），直接试 h2s_2。"""
        h2s_1 = _make_listing("h2s-1", source="holland2stay")
        od_1 = _make_listing("od-1", source="ourdomain")
        h2s_2 = _make_listing("h2s-2", source="holland2stay")
        candidates = [h2s_1, od_1, h2s_2]
        user = _make_user()

        call_count = [0]
        def fake_try(listing, *a, **k):
            call_count[0] += 1
            if call_count[0] == 1:
                return RawBookingResult(listing, success=False, message="raced", phase="race_lost")
            return RawBookingResult(listing, success=True, message="ok",
                                    pay_url="http://pay", phase="success")

        with patch("bookers.holland2stay.try_book", side_effect=fake_try):
            result = book_with_fallback(candidates, user, deadline=float("inf"))

        # 应该调用 try_book 2 次（h2s_1 race_lost → h2s_2 success），跳过 od_1
        assert call_count[0] == 2
        assert result.success is True


# ──────────────────────────────────────────────────────────────────
# HollandStayBooker 转发契约
# ──────────────────────────────────────────────────────────────────


class TestHollandStayBookerForwarding:
    def test_forwards_user_auto_book_fields(self):
        listing = _make_listing("h2s-99")
        user = UserConfig(
            name="b", id="b1b1b1b1",
            auto_book=AutoBookConfig(
                enabled=True, email="b@b.com", password="bpw",
                cancel_enabled=True,
                payment_method="custom_method",
                dry_run=False,
            ),
        )
        booker = HollandStayBooker()
        with patch("bookers.holland2stay.try_book") as mock:
            mock.return_value = RawBookingResult(listing, success=True, message="ok",
                                                 phase="success")
            booker.book(BookingRequest(listing=listing, user=user, dry_run=False))

        args, kwargs = mock.call_args
        assert args[0] is listing
        assert args[1] == "b@b.com"
        assert args[2] == "bpw"
        assert kwargs["dry_run"] is False
        assert kwargs["cancel_enabled"] is True
        assert kwargs["payment_method"] == "custom_method"

    def test_request_dry_run_is_authoritative(self):
        """BookingRequest.dry_run 是权威值——HollandStayBooker 不再叠加
        user.auto_book.dry_run。调用方 (book_with_fallback) 已经把 user 设置
        合并好了，避免"打开 dry_run 后无法关掉"。"""
        listing = _make_listing("h2s-100")
        user = UserConfig(
            name="c", id="c1c1c1c1",
            auto_book=AutoBookConfig(enabled=True, email="c@c.com", password="cpw",
                                     dry_run=True),
        )
        # user.dry_run=True 但 request.dry_run=False → 最终走非 dry_run
        # (实际生产中 book_with_fallback 会先合并 user 设置，但 booker 本身只看 request)
        with patch("bookers.holland2stay.try_book") as mock:
            mock.return_value = RawBookingResult(listing, success=True, message="ok",
                                                 phase="success")
            HollandStayBooker().book(
                BookingRequest(listing=listing, user=user, dry_run=False)
            )
        _, kwargs = mock.call_args
        assert kwargs["dry_run"] is False
