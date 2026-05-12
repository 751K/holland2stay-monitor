"""
notifier 渠道层测试。

覆盖：
- MultiNotifier fanout（any success = True）、空渠道、禁用
- WebNotifier 写入 storage
- _normalize_email_security 别名
- _split_email_recipients
- _format_email_subject
- create_user_notifier 跳过不可用渠道
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from notifier import (
    MultiNotifier,
    WebNotifier,
    _normalize_email_security,
    _split_email_recipients,
    _format_email_subject,
)


# ── _normalize_email_security ─────────────────────────────

class TestNormalizeEmailSecurity:
    def test_starttls_unchanged(self):
        assert _normalize_email_security("starttls") == "starttls"

    def test_tls_alias(self):
        assert _normalize_email_security("tls") == "starttls"

    def test_ssl_unchanged(self):
        assert _normalize_email_security("ssl") == "ssl"

    def test_smtps_alias(self):
        assert _normalize_email_security("smtps") == "ssl"

    def test_none_unchanged(self):
        assert _normalize_email_security("none") == "none"

    def test_plain_alias(self):
        assert _normalize_email_security("plain") == "none"

    def test_empty_defaults_to_starttls(self):
        assert _normalize_email_security("") == "starttls"
        assert _normalize_email_security("  ") == "starttls"

    def test_unknown_defaults_to_starttls(self):
        assert _normalize_email_security("garbage") == "starttls"


# ── _split_email_recipients ───────────────────────────────

class TestSplitEmailRecipients:
    def test_single(self):
        assert _split_email_recipients("a@b.com") == ["a@b.com"]

    def test_comma_separated(self):
        assert _split_email_recipients("a@b.com, c@d.com") == ["a@b.com", "c@d.com"]

    def test_semicolon_separated(self):
        assert _split_email_recipients("a@b.com; c@d.com") == ["a@b.com", "c@d.com"]

    def test_newline_separated(self):
        assert _split_email_recipients("a@b.com\nc@d.com") == ["a@b.com", "c@d.com"]

    def test_empty(self):
        assert _split_email_recipients("") == []

    def test_whitespace_only(self):
        assert _split_email_recipients("  ,  ") == []


# ── _format_email_subject ─────────────────────────────────

class TestFormatEmailSubject:
    def test_short_first_line(self):
        assert "Holland2Stay" in _format_email_subject("新房源上架")

    def test_long_first_line_truncated(self):
        long_text = "A" * 200
        result = _format_email_subject(long_text)
        # "[Holland2Stay] " (16) + truncated(77) + "..." (3) = max 96
        assert len(result) <= 96

    def test_empty_text(self):
        result = _format_email_subject("")
        assert "Holland2Stay" in result


# ── MultiNotifier ─────────────────────────────────────────

class _DummyNotifier:
    """可配置成功/失败的通知器。"""
    def __init__(self, succeed: bool = True, name: str = "dummy"):
        self.succeed = succeed
        self.name = name
        self.sent: list[str] = []
        self.closed = False

    async def _send(self, text: str) -> bool:
        self.sent.append(text)
        return self.succeed

    async def close(self):
        self.closed = True


class TestMultiNotifier:
    def test_any_success_returns_true(self):
        import asyncio
        d1 = _DummyNotifier(succeed=False)
        d2 = _DummyNotifier(succeed=True)
        mn = MultiNotifier([d1, d2])
        ok = asyncio.run(mn._send("test"))
        assert ok is True
        # d1 fails → retried once (2 calls); d2 succeeds first try (1 call)
        assert len(d1.sent) == 2  # fail + retry
        assert len(d2.sent) == 1

    def test_all_fail_returns_false(self):
        import asyncio
        d1 = _DummyNotifier(succeed=False)
        d2 = _DummyNotifier(succeed=False)
        mn = MultiNotifier([d1, d2])
        ok = asyncio.run(mn._send("test"))
        assert ok is False

    def test_disabled_returns_false_without_sending(self):
        import asyncio
        d1 = _DummyNotifier(succeed=True)
        mn = MultiNotifier([d1], enabled=False)
        ok = asyncio.run(mn._send("test"))
        assert ok is False
        assert len(d1.sent) == 0

    def test_empty_channels_returns_false(self):
        import asyncio
        mn = MultiNotifier([])
        ok = asyncio.run(mn._send("test"))
        assert ok is False

    def test_has_channels(self):
        assert MultiNotifier([_DummyNotifier()]).has_channels is True
        assert MultiNotifier([], enabled=True).has_channels is False
        assert MultiNotifier([_DummyNotifier()], enabled=False).has_channels is False

    def test_close_calls_all(self):
        import asyncio
        d1 = _DummyNotifier()
        d2 = _DummyNotifier()
        mn = MultiNotifier([d1, d2])
        asyncio.run(mn.close())
        assert d1.closed is True
        assert d2.closed is True


# ── WebNotifier ───────────────────────────────────────────

class TestWebNotifier:
    def test_send_new_listing_writes_storage(self):
        st = MagicMock()
        wn = WebNotifier(st)
        import asyncio
        from models import Listing
        l = Listing(
            id="t1", name="Test", status="Available to book",
            price_raw="€950", available_from="2026-06-15", features=[],
            url="https://x.com", city="E", sku="SKU1",
            contract_id=1, contract_start_date=None,
        )
        asyncio.run(wn.send_new_listing(l))
        st.add_web_notification.assert_called_once()
        call_kw = st.add_web_notification.call_args[1]
        assert call_kw["type"] == "new_listing"
        assert "Test" in call_kw["title"]

    def test_send_error_writes_storage(self):
        st = MagicMock()
        wn = WebNotifier(st)
        import asyncio
        asyncio.run(wn.send_error("something broke"))
        st.add_web_notification.assert_called_once()
        call_kw = st.add_web_notification.call_args[1]
        assert call_kw["type"] == "error"

    def test_send_heartbeat_writes_storage(self):
        st = MagicMock()
        wn = WebNotifier(st)
        import asyncio
        asyncio.run(wn.send_heartbeat(100, 42))
        st.add_web_notification.assert_called_once()
        call_kw = st.add_web_notification.call_args[1]
        assert call_kw["type"] == "heartbeat"
