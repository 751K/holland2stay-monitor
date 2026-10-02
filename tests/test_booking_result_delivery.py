"""预订结果的送达：成功和失败都推 App；渠道和推送任一送达就算送达。

起因（2026-10-01）
------------------
Laurentina_18101 / Lan 两个用户只用 App（``notification_channels_json = []``），
一天刷了 5 条「自动预订成功但通知发送失败」CRITICAL。查下来两层问题：

1. 预订结果**根本不推 App**：``push.dispatch(kind="booked")`` 有定义没人调。
   那天给他们的 APNs 是同一轮的新房源推送，不是预订结果。
2. 送达判断只看 ``notifier.send_booking_success``（用户自己配的渠道），渠道
   为空就恒为 False——每次成功都报假 CRITICAL，真送不到时反而被淹掉。

``_process_booking_results`` 此前只有一条 grep 源码的测试，这里补行为测试。
"""
from __future__ import annotations

import asyncio
import logging

import pytest

import monitor
from booker import BookingResult
from models import Listing


def _listing(source="holland2stay", lid="L1"):
    return Listing(id=lid, name="Teststraat 1", status="Available to book",
                   price_raw="€900", available_from="2026-11-01", features=[],
                   url="https://example/l", city="Eindhoven", source=source)


class _User:
    def __init__(self, uid="u1", name="App_Only"):
        self.id, self.name = uid, name


class _Notifier:
    """用户自己配的渠道。``ok=False`` 就是渠道列表为空时的行为。"""

    def __init__(self, ok: bool):
        self.ok = ok
        self.success_calls = 0

    async def send_booking_success(self, *a, **kw):
        self.success_calls += 1
        return self.ok

    async def send_booking_failed(self, *a, **kw):
        return self.ok


class _Push:
    def __init__(self, devices_ok: int = 1, boom: bool = False):
        self.devices_ok, self.boom = devices_ok, boom
        self.calls: list[tuple] = []

    async def dispatch(self, storage, user, listing, *, kind="new"):
        self.calls.append((user.id, listing.id, kind))
        if self.boom:
            raise RuntimeError("apns down")
        return self.devices_ok

    async def dispatch_admin(self, storage, msg, *, kind=""):
        self.calls.append(("__admin__", "", kind))
        return 0


class _Storage:
    def mark_listing_reserved_after_booking(self, lid):
        return False


def _run(listing, *, channel_ok, push, success=True, phase=None):
    loop = asyncio.new_event_loop()
    try:
        fut = loop.create_future()
        fut.set_result(BookingResult(
            listing=listing, success=success, message="m", pay_url="https://pay/x",
            phase=phase or ("success" if success else "race_lost")))
        user = _User()
        notifier = _Notifier(channel_ok)

        async def _go():
            tasks = await monitor._process_booking_results(
                [(user, notifier, [listing], fut, None)], None, _Storage(), push,
            )
            # run_once 末尾会 gather 这些；这里照做，否则失败推送根本没跑
            await asyncio.gather(*tasks, return_exceptions=True)

        loop.run_until_complete(_go())
        return notifier
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _clean_retry_queue():
    monitor.retry_queue.discard("u1", "L1")
    yield
    monitor.retry_queue.discard("u1", "L1")


def _criticals(caplog):
    return [r for r in caplog.records if r.levelno >= logging.CRITICAL]


class TestBookedPushIsSent:
    def test_success_dispatches_booked_push(self):
        push = _Push()
        _run(_listing(), channel_ok=True, push=push)
        assert push.calls == [("u1", "L1", "booked")]

    def test_failure_pushes_booking_failed_not_booked(self):
        push = _Push()
        _run(_listing(), channel_ok=True, push=push, success=False)
        assert push.calls == [("u1", "L1", "booking_failed")]

    def test_dry_run_pushes_nothing(self):
        push = _Push()
        loop = asyncio.new_event_loop()
        try:
            fut = loop.create_future()
            fut.set_result(BookingResult(listing=_listing(), success=True, message="m",
                                         dry_run=True, phase="dry_run"))
            loop.run_until_complete(monitor._process_booking_results(
                [(_User(), _Notifier(True), [_listing()], fut, None)], None, _Storage(), push))
        finally:
            loop.close()
        assert push.calls == []


class TestFailurePushOnEveryUserFacingPath:
    """用户侧「没订上」的文本通知有三处发出点，推送得跟着每一处。"""

    def test_blocked(self, monkeypatch):
        monkeypatch.setattr(monitor, "_should_notify_block", lambda: True)
        monkeypatch.setattr(monitor, "_mark_h2s_login_blocked", lambda *_: None)
        push = _Push()
        _run(_listing(), channel_ok=False, push=push, success=False, phase="blocked")
        assert ("u1", "L1", "booking_failed") in push.calls

    def test_blocked_inside_throttle_window_stays_silent(self, monkeypatch):
        """节流期内文本通知不发，推送也不该单独发。"""
        monkeypatch.setattr(monitor, "_should_notify_block", lambda: False)
        monkeypatch.setattr(monitor, "_mark_h2s_login_blocked", lambda *_: None)
        push = _Push()
        _run(_listing(), channel_ok=False, push=push, success=False, phase="blocked")
        assert push.calls == []

    def test_operation_rejected(self, monkeypatch):
        monkeypatch.setattr(monitor, "_should_notify_operation_rejected", lambda: False)
        push = _Push()
        _run(_listing(), channel_ok=False, push=push, success=False,
             phase="operation_rejected")
        assert push.calls == [("u1", "L1", "booking_failed")]


class TestDeliveredCountsPush:
    def test_app_only_user_pushed_ok_is_not_critical(self, caplog):
        """2026-10-01 的形状：渠道为空，推送送达 → 不该有 CRITICAL。"""
        caplog.set_level(logging.INFO)
        _run(_listing(), channel_ok=False, push=_Push(devices_ok=1))
        assert _criticals(caplog) == []

    def test_neither_delivered_is_critical(self, caplog):
        caplog.set_level(logging.INFO)
        _run(_listing(), channel_ok=False, push=_Push(devices_ok=0))
        crit = _criticals(caplog)
        assert len(crit) == 1 and "https://pay/x" in crit[0].getMessage()

    def test_push_exception_counts_as_not_delivered(self, caplog):
        """推送抛异常不能带走整个循环，也不能被当成送达。"""
        caplog.set_level(logging.INFO)
        _run(_listing(), channel_ok=False, push=_Push(boom=True))
        assert len(_criticals(caplog)) == 1

    def test_channel_ok_without_devices_is_fine(self, caplog):
        caplog.set_level(logging.INFO)
        _run(_listing(), channel_ok=True, push=_Push(devices_ok=0))
        assert _criticals(caplog) == []

    def test_plaza_not_delivered_is_warning_not_critical(self, caplog):
        caplog.set_level(logging.INFO)
        _run(_listing(source="plaza"), channel_ok=False, push=_Push(devices_ok=0))
        assert _criticals(caplog) == []
        assert any("注册已提交但通知发送失败" in r.getMessage()
                   for r in caplog.records if r.levelno == logging.WARNING)


class TestBookedPushPayload:
    def test_plaza_says_registration_not_payment(self):
        from mcore.push import _fcm_payload_booked, _payload_booked

        for lang, title, banned in (("en", "Registration submitted", "pay"),
                                    ("zh", "注册已提交", "支付")):
            aps = _payload_booked(_listing(source="plaza"), lang=lang)["aps"]["alert"]
            assert title in aps["title"] and banned not in aps["body"], lang
            fcm = _fcm_payload_booked(_listing(source="plaza"), lang=lang)["message"]["data"]
            assert title in fcm["title"] and banned not in fcm["body"], lang

    def test_hold_sources_keep_pay_wording(self):
        from mcore.push import _payload_booked
        from models import BOOKING_HOLD_SOURCES

        for src in BOOKING_HOLD_SOURCES:
            aps = _payload_booked(_listing(source=src), lang="en")["aps"]["alert"]
            assert "Booking successful" in aps["title"] and "pay" in aps["body"], src


class TestBookingFailedPayload:
    def test_wording_by_platform_and_no_reason_leak(self):
        from mcore.push import _fcm_payload_booking_failed, _payload_booking_failed

        cases = [("holland2stay", "en", "Booking failed", "book manually"),
                 ("holland2stay", "zh", "预订失败", "手动预订"),
                 ("plaza", "en", "Registration failed", "register manually"),
                 ("plaza", "zh", "注册失败", "手动注册")]
        for src, lang, title, body in cases:
            p = _payload_booking_failed(_listing(source=src), lang=lang)
            assert p["kind"] == "booking_failed"
            assert title in p["aps"]["alert"]["title"], (src, lang)
            assert body in p["aps"]["alert"]["body"], (src, lang)
            d = _fcm_payload_booking_failed(_listing(source=src), lang=lang)["message"]["data"]
            assert d["kind"] == "booking_failed" and title in d["title"] and body in d["body"]
            assert d["deep_link"] == "h2smonitor://listing/L1"


class TestBookedPushNotRateLimited:
    def test_booked_bypasses_per_user_limit_but_not_dedup(self):
        from mcore import push

        push.reset()
        try:
            for i in range(push._PER_USER_LIMIT):
                assert push._allow_send("u1", f"new{i}", "new")
            assert not push._allow_send("u1", "newX", "new")        # 新房源被限速
            assert push._allow_send("u1", "L1", "booked")           # 预订结果照发
            assert not push._allow_send("u1", "L1", "booked")       # 去重仍生效
            assert push._allow_send("u1", "L2", "booking_failed")   # 失败同样豁免
        finally:
            push.reset()
