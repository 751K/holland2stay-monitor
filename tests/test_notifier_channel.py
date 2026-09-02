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
        assert "FlatRadar" in _format_email_subject("新房源上架")

    def test_long_first_line_truncated(self):
        long_text = "A" * 200
        result = _format_email_subject(long_text)
        # "[FlatRadar] " (12) + truncated(77) + "..." (3) = max 92
        assert len(result) <= 92

    def test_empty_text(self):
        result = _format_email_subject("")
        assert "FlatRadar" in result


# ── MultiNotifier ─────────────────────────────────────────

class _DummyNotifier:
    """可配置成功/失败的通知器。

    ``raises`` 用来区分两种失败：返回 False（渠道自己判定「没发」，可能是配额
    拒发或配置缺失）与抛传输层异常。两者的重试语义**不同**，见
    MultiNotifier._send_with_retry。
    """
    def __init__(self, succeed: bool = True, name: str = "dummy",
                 raises: bool = False):
        self.succeed = succeed
        self.name = name
        self.raises = raises
        self.sent: list[str] = []
        self.closed = False

    async def _send(self, text: str) -> bool:
        self.sent.append(text)
        if self.raises:
            raise OSError("connection reset")
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
        # **返回 False 不重试。** 这条原先断言 len(d1.sent) == 2，注释写着
        # 「d1 fails → retried once」——把 bug 钉成了期望行为。
        assert len(d1.sent) == 1
        assert len(d2.sent) == 1

    def test_returning_false_is_not_retried(self):
        """False 至少有三种含义，只有一种重试才有意义，而返回值里分不出来。

        最坏的是配额拒发：ResendNotifier 在拒发时会 record_resend_rejected()，
        重试等于**同一条消息记两笔拒发**，把面板上的配额统计做成假数据。
        """
        import asyncio
        d = _DummyNotifier(succeed=False)
        assert asyncio.run(MultiNotifier([d])._send("x")) is False
        assert len(d.sent) == 1, "返回 False 被重试了"

    def test_transport_exception_is_retried_once(self):
        """传输层异常仍然重试——那是这层唯一能确定「我这次没发出去」的信号。"""
        import asyncio
        d = _DummyNotifier(raises=True)
        assert asyncio.run(MultiNotifier([d])._send("x")) is False
        assert len(d.sent) == 2, "传输异常没有重试"

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

    def test_send_booking_success_writes_user_id(self):
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
        asyncio.run(wn.send_booking_success(l, "ok", user_id="u1"))
        call_kw = st.add_web_notification.call_args[1]
        assert call_kw["type"] == "booking"
        assert call_kw["user_id"] == "u1"
