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
    _format_email_html,
    _format_email_subject,
    _format_telegram_html,
    TelegramNotifier,
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
        assert "[H2S] New Listing" in text
        assert "Teststraat 1-A, Eindhoven" in text
        assert "Status: Available to book" in text
        assert "€950" in text
        assert "2026-06-15" in text
        assert "Type: Studio" in text
        assert "Area: 30.0 m²" in text
        assert "Occupancy: Single" in text
        assert "https://www.holland2stay.com/residences/test-1.html" in text

    def test_missing_available_from(self):
        text = _format_new(_listing(available_from=None))
        assert "Available: ?" in text

    def test_empty_features(self):
        text = _format_new(_listing(features=[]))
        assert "Type:" not in text
        assert "Area:" not in text

    def test_contains_price_per_month(self):
        text = _format_new(_listing())
        assert "/mo" in text

    def test_ourdomain_listing_uses_short_platform_badge(self):
        text = _format_new(_listing(source="ourdomain"))
        assert "[OD] New Listing" in text


# ── _format_status_change ─────────────────────────────────

class TestFormatStatusChange:
    def test_lottery_to_book(self):
        text = _format_status_change(
            _listing(), "Available in lottery", "Available to book"
        )
        assert "[H2S] Status Change" in text
        assert "Available in lottery → Available to book" in text

    def test_book_to_not_available(self):
        text = _format_status_change(
            _listing(status="Not available"),
            "Available to book", "Not available",
        )
        assert "[H2S] Status Change" in text
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
        assert "[H2S] Booking Successful!" in text
        assert "idealcheckout" in text

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
        assert "[H2S] Booking Failed" in text
        assert "already booked by someone else" in text
        assert "Manual booking:" in text
        assert "https://www.holland2stay.com/residences/test-1.html" in text

    def test_with_reserved_conflict(self):
        text = _format_booking_failed(
            _listing(), "another unit reserved",
        )
        assert "another unit reserved" in text


class TestEmailFormatting:
    def test_subject_uses_flatradar_brand(self):
        subject = _format_email_subject("FlatRadar Monitor\nbody")
        assert subject == "[FlatRadar] FlatRadar Monitor"
        assert "Holland2Stay" not in subject

    def test_html_template_escapes_dynamic_text(self):
        html = _format_email_html("[H2S] New Listing\n\n<script>alert(1)</script>")
        assert "FlatRadar" in html
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


class TestTelegramFormatting:
    def test_telegram_html_is_branded_and_less_demo_like(self):
        html = _format_telegram_html(_format_new(_listing()))

        assert "<b>FlatRadar</b>" in html
        assert "<b>[H2S] New Listing</b>" in html
        assert "Status: Available to book" in html
        assert "✅" not in html
        assert '<a href="https://www.holland2stay.com/residences/test-1.html">' in html

    def test_telegram_html_escapes_dynamic_content(self):
        html = _format_telegram_html(
            _format_booking_failed(
                _listing(name="<script>alert(1)</script>"),
                '<b onclick="x">bad</b>',
            )
        )

        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
        assert '<b onclick="x">bad</b>' not in html
        assert "&lt;b onclick=&quot;x&quot;&gt;bad&lt;/b&gt;" in html

    def test_telegram_post_uses_html_parse_mode(self):
        class _Resp:
            ok = True
            status_code = 200
            text = "ok"

        class _Session:
            payload = None

            def post(self, url, json, timeout):
                self.payload = json
                return _Resp()

            def close(self):
                pass

        notifier = TelegramNotifier("token", "chat")
        fake = _Session()
        notifier._session = fake

        assert notifier._post("https://api.telegram.org/bottoken/sendMessage", "✅ 标题\n\n🔗 https://example.com") is True
        assert fake.payload["parse_mode"] == "HTML"
        assert fake.payload["disable_web_page_preview"] is True
        assert "<b>FlatRadar</b>" in fake.payload["text"]
