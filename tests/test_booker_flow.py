"""
booker.py try_book 链路测试。

覆盖（不依赖外部网络）：
- 非 Available to book → 立即拒绝
- BookingResult dataclass 字段
- expired prewarmed → 回退到正常登录
- _fetch_sku_and_contract 需 mock（curl_cffi Session）
"""
from __future__ import annotations

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
        # STATUS_AVAILABLE = "available to book", lower() comparison
        assert l.status.lower() == STATUS_AVAILABLE

    def test_dry_run_with_prewarmed_skips_login(self):
        """prewarmed session + dry_run → 跳过登录直接返回成功。"""
        import time
        now = time.monotonic()
        l = _listing()
        ps = PrewarmedSession(
            session=MagicMock(), token="tok", created_at=now,
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

def _mock_login_response():
    return {"data": {"generateCustomerToken": {"token": "mock_token"}}}


class TestPrewarmedFallback:
    def test_expired_prewarmed_closes_old_session(self, monkeypatch):
        """过期 prewarmed → 关闭旧 session。"""
        import curl_cffi.requests as req

        mock = MagicMock()
        mock.post.return_value.ok = True
        mock.post.return_value.json.return_value = _mock_login_response()
        monkeypatch.setattr(req, "Session", lambda **kw: mock)

        l = _listing()
        old_session = MagicMock()
        expired_ps = PrewarmedSession(
            session=old_session, token="old_token", created_at=0.0,
            token_expiry=0.0, email="test@x.com",
        )
        try_book(l, "test@x.com", "pw", prewarmed=expired_ps)
        old_session.close.assert_called_once()

    def test_valid_prewarmed_reused(self, monkeypatch):
        """有效 prewarmed → 复用，不关闭。"""
        import time
        import curl_cffi.requests as req

        mock = MagicMock()
        mock.post.return_value.ok = True
        mock.post.return_value.json.return_value = _mock_login_response()
        monkeypatch.setattr(req, "Session", lambda **kw: mock)

        now = time.monotonic()
        l = _listing()
        session = MagicMock()
        valid_ps = PrewarmedSession(
            session=session, token="t", created_at=now,
            token_expiry=now + 99999, email="test@x.com",
        )
        try_book(l, "test@x.com", "pw", prewarmed=valid_ps)
        session.close.assert_not_called()


# ── PrewarmedSession ──────────────────────────────────────

class TestPrewarmedSession:
    def test_attrs(self):
        import time
        now = time.monotonic()
        s = MagicMock()
        ps = PrewarmedSession(session=s, token="tok", created_at=now,
                              token_expiry=now + _TOKEN_MAX_AGE,
                              email="a@b.com")
        assert ps.email == "a@b.com"
        assert ps.token == "tok"
        assert ps.token_expiry - now == _TOKEN_MAX_AGE
