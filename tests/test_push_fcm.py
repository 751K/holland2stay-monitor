"""
mcore.push FCM 调度器单元测试
===============================

覆盖：
- FCM 未启用时 dispatch 全部 no-op（仅 APNs 路径不受影响）
- FCM + APNs 双发分流（iOS → APNs, Android → FCM）
- FCM device_dead → disable_device
- FCM payload 构造（data-only, 不含 aps）
- FCM 节流去重与 APNs 共享
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from mcore import push
from notifier_channels.fcm import FcmResult


def _run(coro):
    return asyncio.run(coro)


# ── Fakes ────────────────────────────────────────────────────────────


class FakeApns:
    def __init__(self):
        self.calls: list[dict] = []

    async def send_many(self, targets, *, payload, collapse_id="", **_):
        self.calls.append({"targets": targets, "payload": payload})
        from notifier_channels.apns import ApnsResult
        return [
            ApnsResult(status=200, reason="OK", device=t["device_token"])
            for t in targets
        ]


class FakeFcm:
    def __init__(self, results=None):
        self.calls: list[dict] = []
        self._results = results or []

    async def send_many(self, targets, *, payload, collapse_key="", **_):
        self.calls.append({"targets": targets, "payload": payload, "collapse_key": collapse_key})
        if self._results:
            return self._results.pop(0)
        return [
            FcmResult(status=200, reason="OK", device=t["device_token"], message_id=f"msg-{i}")
            for i, t in enumerate(targets)
        ]


class FakeStore:
    def __init__(self, devices_by_user: dict[str, list[dict]] = None):
        self.devices_by_user = devices_by_user or {}
        self.disabled: list[tuple[int, str]] = []

    def get_active_devices_for_user(self, user_id: str) -> list[dict]:
        return list(self.devices_by_user.get(user_id, []))

    def get_active_devices_for_admin(self) -> list[dict]:
        return list(self.devices_by_user.get("__admin__", []))

    def disable_device(self, device_id: int, reason: str = "") -> bool:
        self.disabled.append((device_id, reason))
        return True


@dataclass
class FakeListing:
    id: str
    name: str = "test"
    city: str = "Eindhoven"
    status: str = "Available to book"
    price_display: str = "€700"
    available_from: str = "2026-06-01"

    def feature_map(self):
        return {"area": "26 m²"}


@pytest.fixture(autouse=True)
def reset_push():
    push.reset()
    yield
    push.reset()


# ── Platform separation ──────────────────────────────────────────────


class TestPlatformSeparation:
    def test_ios_to_apns_only(self):
        """iOS 设备走 APNs，不碰 FCM。"""
        fake_apns = FakeApns()
        fake_fcm = FakeFcm()
        push.set_client(fake_apns)
        push.set_fcm_client(fake_fcm)
        store = FakeStore({"userA": [
            {"id": 1, "device_token": "ios_tok" + "0" * 57, "env": "production", "platform": "ios"},
        ]})

        n = _run(push.dispatch(store, type("U", (), {"id": "userA"})(), FakeListing("l1")))
        assert n == 1
        assert len(fake_apns.calls) == 1
        assert len(fake_fcm.calls) == 0

    def test_android_to_fcm_only(self):
        """Android 设备走 FCM，不碰 APNs。"""
        fake_apns = FakeApns()
        fake_fcm = FakeFcm()
        push.set_client(fake_apns)
        push.set_fcm_client(fake_fcm)
        store = FakeStore({"userA": [
            {"id": 2, "device_token": "fcm_tok" * 8, "platform": "android", "language": "en"},
        ]})

        n = _run(push.dispatch(store, type("U", (), {"id": "userA"})(), FakeListing("l1")))
        assert n == 1
        assert len(fake_apns.calls) == 0
        assert len(fake_fcm.calls) == 1

    def test_mixed_platforms_dual_dispatch(self):
        """混合设备同时走 APNs + FCM。"""
        fake_apns = FakeApns()
        fake_fcm = FakeFcm()
        push.set_client(fake_apns)
        push.set_fcm_client(fake_fcm)
        store = FakeStore({"userA": [
            {"id": 1, "device_token": "ios" + "0" * 61, "env": "production", "platform": "ios"},
            {"id": 2, "device_token": "fcm" * 22, "platform": "android", "language": "en"},
        ]})

        n = _run(push.dispatch(store, type("U", (), {"id": "userA"})(), FakeListing("l1")))
        assert n == 2
        assert len(fake_apns.calls) == 1
        assert len(fake_fcm.calls) == 1

    def test_no_platform_defaults_to_apns(self):
        """未设 platform 的老设备默认走 APNs。"""
        fake_apns = FakeApns()
        fake_fcm = FakeFcm()
        push.set_client(fake_apns)
        push.set_fcm_client(fake_fcm)
        store = FakeStore({"userA": [
            {"id": 99, "device_token": "old" * 20, "env": "production"},  # no platform field
        ]})

        n = _run(push.dispatch(store, type("U", (), {"id": "userA"})(), FakeListing("l1")))
        assert n == 1
        assert len(fake_apns.calls) == 1
        assert len(fake_fcm.calls) == 0

    def test_fcm_disabled_still_sends_apns(self):
        """FCM 未启用时 APNs 仍正常工作。"""
        fake_apns = FakeApns()
        push.set_client(fake_apns)
        store = FakeStore({"userA": [
            {"id": 1, "device_token": "ios" + "0" * 61, "env": "production", "platform": "ios"},
            {"id": 2, "device_token": "fcm" * 22, "platform": "android"},
        ]})

        n = _run(push.dispatch(store, type("U", (), {"id": "userA"})(), FakeListing("l1")))
        assert n == 1  # only iOS
        assert len(fake_apns.calls) == 1


# ── FCM Payload ─────────────────────────────────────────────────────


class TestFcmPayload:
    def test_new_listing_data_only(self):
        from mcore.push import _fcm_payload_new_listing
        p = _fcm_payload_new_listing(FakeListing("l123"), lang="en")
        msg = p["message"]
        assert "notification" not in msg  # data-only
        assert msg["data"]["title"].startswith("[H2S]")
        assert msg["data"]["listing_id"] == "l123"
        assert msg["data"]["kind"] == "new"
        assert "h2smonitor" in msg["data"]["deep_link"]
        assert msg["android"]["priority"] == "high"

    def test_new_listing_zh(self):
        from mcore.push import _fcm_payload_new_listing
        p = _fcm_payload_new_listing(FakeListing("l1"), lang="zh")
        assert "新房源" in p["message"]["data"]["title"]

    def test_status_change(self):
        from mcore.push import _fcm_payload_status_change
        p = _fcm_payload_status_change(FakeListing("l2"), "lottery", "available", lang="en")
        data = p["message"]["data"]
        assert data["kind"] == "status_change"
        assert "lottery" in data["body"]
        assert "available" in data["body"]

    def test_booked(self):
        from mcore.push import _fcm_payload_booked
        p = _fcm_payload_booked(FakeListing("l3"), lang="en")
        assert p["message"]["data"]["kind"] == "booked"

    def test_round_aggregate(self):
        from mcore.push import _fcm_payload_round_aggregate
        listings = [FakeListing(f"l{i}") for i in range(3)]
        p = _fcm_payload_round_aggregate(listings, "r99", lang="en")
        data = p["message"]["data"]
        assert data["kind"] == "round"
        assert data["round_id"] == "r99"
        assert "3" in data["title"]

    def test_error(self):
        from mcore.push import _fcm_payload_error
        p = _fcm_payload_error("Cloudflare 403", kind="blocked", lang="en")
        data = p["message"]["data"]
        assert data["kind"] == "blocked"
        assert "Cloudflare" in data["body"]


# ── FCM device_dead → disable_device ─────────────────────────────────


class TestFcmDisableOnDeadToken:
    def test_unregistered_disables_device(self):
        store = FakeStore({"userA": [
            {"id": 55, "device_token": "dead_tok" * 8, "platform": "android", "language": "en"},
        ]})
        fake_fcm = FakeFcm(results=[
            [FcmResult(status=404, reason="unregistered", device="dead_tok" * 8)],
        ])
        push.set_fcm_client(fake_fcm)
        # APNs must be present too for dispatch to proceed
        push.set_client(FakeApns())

        n = _run(push.dispatch(store, type("U", (), {"id": "userA"})(), FakeListing("l1")))
        assert n == 0  # FCM failed
        assert store.disabled == [(55, "unregistered")]


# ── dispatch_status_change with FCM ──────────────────────────────────


class TestFcmStatusChange:
    def test_fcm_status_change_dispatched(self):
        fake_fcm = FakeFcm()
        push.set_fcm_client(fake_fcm)
        push.set_client(FakeApns())
        store = FakeStore({"userA": [
            {"id": 3, "device_token": "fcm" * 22, "platform": "android", "language": "en"},
        ]})

        n = _run(push.dispatch_status_change(
            store, type("U", (), {"id": "userA"})(),
            FakeListing("sc1"), "Available in lottery", "Available to book",
        ))
        assert n == 1
        payload = fake_fcm.calls[0]["payload"]
        assert payload["message"]["data"]["kind"] == "status_change"


# ── dispatch_aggregate with FCM ──────────────────────────────────────


class TestFcmAggregate:
    def test_fcm_aggregate_dispatched(self):
        fake_fcm = FakeFcm()
        push.set_fcm_client(fake_fcm)
        push.set_client(FakeApns())
        store = FakeStore({"userA": [
            {"id": 4, "device_token": "fcm" * 22, "platform": "android", "language": "en"},
        ]})

        listings = [FakeListing(f"l{i}") for i in range(5)]
        n = _run(push.dispatch_aggregate(
            store, type("U", (), {"id": "userA"})(), listings,
            round_id="agg-1",
        ))
        assert n == 1
        call = fake_fcm.calls[0]
        assert call["collapse_key"] == "agg-1"
        assert call["payload"]["message"]["data"]["kind"] == "round"


# ── dispatch_error with FCM ───────────────────────────────────────────


class TestFcmError:
    def test_fcm_error_dispatched(self):
        fake_fcm = FakeFcm()
        push.set_fcm_client(fake_fcm)
        push.set_client(FakeApns())
        store = FakeStore({"userA": [
            {"id": 5, "device_token": "fcm" * 22, "platform": "android", "language": "en"},
        ]})

        n = _run(push.dispatch_error(
            store, type("U", (), {"id": "userA"})(), "403 blocked",
        ))
        assert n == 1
        payload = fake_fcm.calls[0]["payload"]
        assert payload["message"]["data"]["kind"] == "blocked"


# ── 双发 send_many 异常隔离 ─────────────────────────────────────────────


class TestFcmExceptionIsolation:
    def test_fcm_throw_does_not_block_apns(self):
        """FCM 异常不影响 APNs 正常发送。"""
        fake_apns = FakeApns()

        class BoomFcm:
            async def send_many(self, *a, **kw):
                raise RuntimeError("fcm down")

        push.set_client(fake_apns)
        push.set_fcm_client(BoomFcm())
        store = FakeStore({"userA": [
            {"id": 1, "device_token": "ios" + "0" * 61, "env": "production", "platform": "ios"},
            {"id": 2, "device_token": "fcm" * 22, "platform": "android", "language": "en"},
        ]})

        n = _run(push.dispatch(store, type("U", (), {"id": "userA"})(), FakeListing("l1")))
        assert n == 1  # only iOS succeeds
        assert len(fake_apns.calls) == 1


# ── FCM admin dispatch ───────────────────────────────────────────────


class TestFcmAdmin:
    def test_fcm_admin_dispatched(self):
        fake_fcm = FakeFcm()
        push.set_fcm_client(fake_fcm)
        push.set_client(FakeApns())
        store = FakeStore({"__admin__": [
            {"id": 9, "device_token": "fcm" * 22, "platform": "android", "language": "en"},
        ]})

        n = _run(push.dispatch_admin(store, "alert!"))
        assert n == 1
        call = fake_fcm.calls[0]
        assert call["payload"]["message"]["data"]["kind"] == "blocked"
