"""
booker.py try_book 链路测试（BrowserFetcher 版）。
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from booker import (
    BookingResult,
    PrewarmedSession,
    _TOKEN_MAX_AGE,
    try_book,
)
from models import STATUS_AVAILABLE, Listing


def _listing(**overrides):
    defaults = {
        "id": "test-1",
        "name": "Test Listing",
        "status": "Available to book",
        "price_raw": "€950",
        "available_from": "2026-06-15",
        "features": ["Type: Studio"],
        "url": "https://example.com",
        "city": "Eindhoven",
        "sku": "SKU001",
        "contract_id": 1,
        "contract_start_date": "2026-06-15",
    }
    defaults.update(overrides)
    return Listing(**defaults)


# ── 快速拒绝路径（无网络） ────────────────────────────────

class TestTryBookReject:
    def test_non_available_status_rejected(self):
        l = _listing(status="Available in lottery")
        result = try_book(l, "test@x.com", "pw")
        assert result.success is False
        assert result.phase == ""

    def test_not_available_rejected(self):
        l = _listing(status="Not available")
        result = try_book(l, "test@x.com", "pw")
        assert result.success is False

    def test_status_case_insensitive(self):
        l = _listing(status="AVAILABLE TO BOOK")
        assert l.status.lower() == STATUS_AVAILABLE

    def test_dry_run_with_prewarmed_skips_login(self):
        now = time.monotonic()
        l = _listing()
        mock_fetcher = MagicMock()
        ps = PrewarmedSession(
            fetcher=mock_fetcher, token="tok", created_at=now,
            token_expiry=now + 99999, email="test@x.com",
        )
        result = try_book(l, "test@x.com", "pw", dry_run=True, prewarmed=ps)
        assert result.success is True
        assert result.dry_run is True
        assert result.phase == "dry_run"
        assert result.pay_url == ""


# ── BookingResult dataclass ───────────────────────────────

class TestBookingResult:
    def test_success_result_fields(self):
        l = _listing()
        br = BookingResult(l, True, "ok", pay_url="https://pay.example.com",
                           contract_start_date="2026-07-01", phase="success")
        assert br.success is True
        assert br.pay_url == "https://pay.example.com"
        assert br.contract_start_date == "2026-07-01"
        assert br.phase == "success"

    def test_failure_result_fields(self):
        l = _listing()
        br = BookingResult(l, False, "race lost", phase="race_lost")
        assert br.success is False
        assert br.pay_url == ""
        assert br.phase == "race_lost"


# ── Expired prewarmed fallback ────────────────────────────

class TestPrewarmedFallback:
    def test_expired_prewarmed_closes_old_fetcher(self):
        """过期 prewarmed → 关闭旧 fetcher。"""
        with patch("booker.BrowserFetcher") as MockFetcher:
            new_mock = MockFetcher.return_value
            new_mock.__enter__.return_value = new_mock
            new_mock.__exit__.return_value = False
            new_mock.fetch_gql.return_value = {
                "data": {"generateCustomerToken": {"token": "mock_token"}}
            }

            l = _listing()
            old_fetcher = MagicMock()
            expired_ps = PrewarmedSession(
                fetcher=old_fetcher, token="old_token", created_at=0.0,
                token_expiry=0.0, email="test@x.com",
            )
            # 会触发 login 然后 add_to_cart 然后 place_order... 都会 fetch_gql
            # 但我们只需要验证 old fetcher 被关闭
            try:
                try_book(l, "test@x.com", "pw", prewarmed=expired_ps)
            except Exception:
                pass  # 后续调用可能失败（mock 不完整），但不影响我们的断言
            old_fetcher.close.assert_called_once()

    def test_valid_prewarmed_reused(self):
        """有效 prewarmed → 复用，不关闭。"""
        now = time.monotonic()
        l = _listing()
        mock_fetcher = MagicMock()
        mock_fetcher.fetch_gql.return_value = {
            "data": {"generateCustomerToken": {"token": "t"}}
        }
        valid_ps = PrewarmedSession(
            fetcher=mock_fetcher, token="t", created_at=now,
            token_expiry=now + 99999, email="test@x.com",
        )
        # 由于后续 booking 步骤也可能失败（mock 不完整），
        # 只验证 fetcher 没有被 close
        try:
            try_book(l, "test@x.com", "pw", prewarmed=valid_ps)
        except Exception:
            pass
        mock_fetcher.close.assert_not_called()


# ── PrewarmedSession ──────────────────────────────────────

class TestPrewarmedSession:
    def test_attrs(self):
        now = time.monotonic()
        s = MagicMock()
        ps = PrewarmedSession(fetcher=s, token="tok", created_at=now,
                              token_expiry=now + _TOKEN_MAX_AGE,
                              email="a@b.com")
        assert ps.email == "a@b.com"
        assert ps.token == "tok"
        assert ps.token_expiry - now == pytest.approx(_TOKEN_MAX_AGE)
