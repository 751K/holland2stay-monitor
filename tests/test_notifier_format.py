"""
notifier 消息格式化测试。

四个 _format_* 函数是纯文本输出，易于验证：
- _format_new         — 新房源通知
- _format_status_change — 状态变更通知
- _format_booking_success — 预订成功通知
- _format_booking_failed  — 预订失败通知
"""
from __future__ import annotations

from models import Listing
from notifier import (
    _format_new,
    _format_status_change,
    _format_booking_success,
    _format_booking_failed,
)


def _listing(**overrides):
    """构造最小 Listing 供格式化测试。"""
    defaults = {
        "id": "test-1",
        "name": "Teststraat 1-A, Eindhoven",
        "status": "Available to book",
        "price_raw": "€950",
        "available_from": "2026-06-15",
        "features": [
            "Type: Studio",
            "Area: 30.0 m²",
            "Occupancy: Single",
            "Floor: 2",
            "Finishing: Upholstered",
            "Energy: A",
            "Neighborhood: Centrum",
            "Building: The Tower",
        ],
        "url": "https://www.holland2stay.com/residences/test-1.html",
        "city": "Eindhoven",
        "sku": "SKU001",
        "contract_id": 1,
        "contract_start_date": None,
    }
    defaults.update(overrides)
    return Listing(**defaults)


# ── _format_new ────────────────────────────────────────────

class TestFormatNew:
    def test_direct_book_listing(self):
        text = _format_new(_listing())
        assert "✅ 新房源上架" in text
        assert "Teststraat 1-A, Eindhoven" in text
        assert "Available to book" in text
        assert "€950" in text
        assert "2026-06-15" in text
        assert "Studio" in text
        assert "30.0 m²" in text
        assert "Single" in text
        assert "Floor: 2" not in text  # floor is in features, not separate line
        assert "https://www.holland2stay.com/residences/test-1.html" in text

    def test_lottery_listing_has_slot_icon(self):
        text = _format_new(_listing(status="Available in lottery"))
        assert "🎰 新房源上架" in text

    def test_missing_available_from(self):
        text = _format_new(_listing(available_from=None))
        assert "未知" in text

    def test_empty_features(self):
        text = _format_new(_listing(features=[]))
        assert "类型" not in text  # no type line when features empty
        assert "面积" not in text

    def test_contains_price_per_month(self):
        text = _format_new(_listing())
        assert "/月" in text


# ── _format_status_change ─────────────────────────────────

class TestFormatStatusChange:
    def test_lottery_to_book(self):
        text = _format_status_change(
            _listing(), "Available in lottery", "Available to book"
        )
        assert "🚀 状态变更" in text
        assert "Available in lottery → Available to book" in text

    def test_book_to_not_available(self):
        text = _format_status_change(
            _listing(status="Not available"),
            "Available to book", "Not available",
        )
        assert "🔄 状态变更" in text
        assert "Available to book → Not available" in text

    def test_contains_listing_url(self):
        text = _format_status_change(
            _listing(), "X", "Y",
        )
        assert "https://www.holland2stay.com/residences/test-1.html" in text


# ── _format_booking_success ────────────────────────────────

class TestFormatBookingSuccess:
    def test_with_pay_url(self):
        text = _format_booking_success(
            _listing(), "detail fallback",
            pay_url="https://account.holland2stay.com/idealcheckout/setup.php?order_id=123",
        )
        assert "🛒 自动预订成功！" in text
        assert "idealcheckout" in text
        assert "链接直达支付页面" in text
        assert "无需登录" in text

    def test_no_pay_url_falls_back_to_detail(self):
        text = _format_booking_success(
            _listing(), "No payment URL available",
        )
        assert "No payment URL available" in text

    def test_contract_start_date_used_over_listing_date(self):
        text = _format_booking_success(
            _listing(available_from="2026-06-15"),
            "ok",
            contract_start_date="2026-07-01",
        )
        assert "2026-07-01" in text
        assert "2026-06-15" not in text

    def test_listing_available_from_as_fallback(self):
        text = _format_booking_success(
            _listing(available_from="2026-08-01"),
            "ok",
        )
        assert "2026-08-01" in text


# ── _format_booking_failed ─────────────────────────────────

class TestFormatBookingFailed:
    def test_with_reason(self):
        text = _format_booking_failed(
            _listing(), "already booked by someone else",
        )
        assert "❌ 自动预订失败" in text
        assert "already booked by someone else" in text
        assert "请手动预订" in text
        assert "https://www.holland2stay.com/residences/test-1.html" in text

    def test_with_reserved_conflict(self):
        text = _format_booking_failed(
            _listing(), "another unit reserved",
        )
        assert "another unit reserved" in text
