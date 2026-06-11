"""
bookers/__init__.py 的 dispatcher 单元测试
==========================================

覆盖三种路径：
1. listing.source="holland2stay" → 路由到 HollandStayBooker（验证转发）
2. listing.source="xior"/"ourdomain" → 路由到 RENTCafe booker
3. listing.source="unknown_xxx" → 返回 phase="unsupported"

以及 mcore.booking.book_with_fallback 过滤候选的回归保护：
- 混合 source 列表：H2S + 未知 source 候选混着进，未知 source 应被过滤、
  不会触发任何 dispatch（重要：避免对不支持平台产生"预订失败"误报）
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

    def test_xior_and_ourdomain_registered(self):
        assert "xior" in BOOKER_REGISTRY
        assert "ourdomain" in BOOKER_REGISTRY
        from bookers.rentcafe import XiorBooker, OurDomainBooker
        assert BOOKER_REGISTRY["xior"] is XiorBooker
        assert BOOKER_REGISTRY["ourdomain"] is OurDomainBooker

    def test_get_booker_h2s_returns_instance(self):
        booker = get_booker("holland2stay")
        assert isinstance(booker, HollandStayBooker)
        assert isinstance(booker, AbstractBooker)

    def test_get_booker_unregistered_returns_none(self):
        assert get_booker("nonsense") is None
        assert get_booker("") is None

    def test_supports_booking(self):
        assert supports_booking("holland2stay") is True
        assert supports_booking("xior") is True
        assert supports_booking("ourdomain") is True
        assert supports_booking("") is False
        assert supports_booking("nonsense") is False


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

    def test_ourdomain_routes_to_rentcafe_booker(self):
        """OurDomain 现在有 RENTCafe booker，应路由到 OurDomainBooker.book()"""
        listing = _make_listing("od-1", source="ourdomain")
        user = _make_user()
        with patch("bookers.rentcafe.OurDomainBooker.book", return_value=RawBookingResult(listing, success=True, message="ok")) as mock:
            result = dispatch_book(
                BookingRequest(listing=listing, user=user)
            )
        mock.assert_called_once()
        assert result.success is True

    def test_xior_routes_to_rentcafe_booker(self):
        listing = _make_listing("xr-1", source="xior")
        user = _make_user()
        with patch("bookers.rentcafe.XiorBooker.book", return_value=RawBookingResult(listing, success=True, message="ok")) as mock:
            result = dispatch_book(
                BookingRequest(listing=listing, user=user)
            )
        mock.assert_called_once()
        assert result.success is True

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
    def test_only_unsupported_candidates_short_circuit(self):
        """全部候选都是未知 source → book_with_fallback 立即返回 None。"""
        candidates = [
            _make_listing("xx-1", source="pararius"),
            _make_listing("xx-2", source="pararius"),
        ]
        user = _make_user()
        with patch("bookers.holland2stay.try_book") as mock:
            result = book_with_fallback(candidates, user, deadline=float("inf"))
        mock.assert_not_called()
        assert result is None

    def test_mixed_supported_and_unsupported(self):
        """H2S + 未知 source 混合：未知 source 跳过，H2S 候选正常尝试。"""
        h2s_1 = _make_listing("h2s-1", source="holland2stay")
        xx_1 = _make_listing("xx-1", source="pararius")
        h2s_2 = _make_listing("h2s-2", source="holland2stay")
        candidates = [h2s_1, xx_1, h2s_2]
        user = _make_user()

        call_listings = []
        def fake_try(listing, *a, **k):
            call_listings.append(listing)
            return RawBookingResult(listing, success=True, message="ok", phase="success")

        with patch("bookers.holland2stay.try_book", side_effect=fake_try):
            result = book_with_fallback(candidates, user, deadline=float("inf"))

        assert len(call_listings) == 1
        assert call_listings[0] is h2s_1
        assert result.success is True

    def test_mixed_h2s_race_lost_skips_unsupported(self):
        """h2s_1 race_lost → 跳过未知 source → 试 h2s_2。"""
        h2s_1 = _make_listing("h2s-1", source="holland2stay")
        xx_1 = _make_listing("xx-1", source="pararius")
        h2s_2 = _make_listing("h2s-2", source="holland2stay")
        candidates = [h2s_1, xx_1, h2s_2]
        user = _make_user()

        call_count = [0]
        def fake_try(listing, *a, **k):
            call_count[0] += 1
            if call_count[0] == 1:
                return RawBookingResult(listing, success=False, message="raced", phase="race_lost")
            return RawBookingResult(listing, success=True, message="ok", phase="success")

        with patch("bookers.holland2stay.try_book", side_effect=fake_try):
            result = book_with_fallback(candidates, user, deadline=float("inf"))

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
