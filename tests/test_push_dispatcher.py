"""
mcore.push 调度器单元测试
==========================

不开网络——用一个 fake ApnsClient 注入：
- 记录 send_many 的调用参数（设备列表、payload、collapse_id）
- 返回可配置的 ApnsResult 列表

覆盖：
- APNs 未启用（get_client→None）时 dispatch 全部 no-op
- dispatch 单条 + dispatch_status_change + dispatch_aggregate + dispatch_error
- 节流去重：同 (user, listing, kind) 5min 内只发 1 次
- 每用户每分钟上限 10 条
- 410 device_dead → disable_device 被调
- should_aggregate 阈值
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from mcore import push
from notifier_channels.apns import ApnsResult
from notifier_channels.fcm import FcmResult


def _run(coro):
    return asyncio.run(coro)


# ── Fake APNs client ────────────────────────────────────────────────


class FakeApns:
    """记录调用 + 可编程的返回值。"""

    def __init__(self, results_per_call: list[list[ApnsResult]] | None = None):
        self.calls: list[dict] = []
        self._results = results_per_call or []

    async def send_many(self, targets, *, payload, collapse_id="", **_):
        self.calls.append({
            "targets": targets,
            "payload": payload,
            "collapse_id": collapse_id,
        })
        if self._results:
            return self._results.pop(0)
        # 默认成功
        return [
            ApnsResult(status=200, reason="OK", device=t["device_token"])
            for t in targets
        ]


# ── Fake storage ────────────────────────────────────────────────────


class FakeStore:
    def __init__(self, devices_by_user: dict[str, list[dict]]):
        self.devices_by_user = devices_by_user
        self.disabled: list[tuple[int, str]] = []

    def get_active_devices_for_user(self, user_id: str) -> list[dict]:
        return list(self.devices_by_user.get(user_id, []))

    def disable_device(self, device_id: int, reason: str = "") -> bool:
        self.disabled.append((device_id, reason))
        return True


# ── Fake user / listing ─────────────────────────────────────────────


@dataclass
class FakeUser:
    id: str
    name: str = "kong"


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


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_push():
    push.reset()
    yield
    push.reset()


@pytest.fixture
def fake_apns():
    return FakeApns()


@pytest.fixture
def with_apns(fake_apns, monkeypatch):
    """注入 fake client 取代 get_client 真实构造。"""
    push.set_client(fake_apns)
    return fake_apns


@pytest.fixture
def store_with_one_device():
    return FakeStore({"userA": [
        {"id": 1, "device_token": "tokA" + "0" * 60, "env": "production"},
    ]})


# ── APNs 未启用 ─────────────────────────────────────────────────────


class TestDisabled:
    def test_no_client_no_op(self, store_with_one_device):
        """get_client 返回 None 时 dispatch 不调 storage、立即 0。"""
        # 不调用 set_client → _client_disabled 通过 from_env 判断
        push.reset()
        # 没有 .env 配置 → from_env 返回 None → 客户端禁用
        n = _run(push.dispatch(
            store_with_one_device, FakeUser("userA"), FakeListing("l1"),
        ))
        assert n == 0

    def test_disabled_client_retries_after_cooldown(self, monkeypatch, store_with_one_device):
        """APNs 首次配置缺失不能让 monitor 进程永久 no-op。"""
        fake = FakeApns()
        calls = 0

        class FakeConfig:
            topic = "com.example.FlatRadar"
            env_default = "production"

        def fake_from_env():
            nonlocal calls
            calls += 1
            return None if calls == 1 else FakeConfig()

        monkeypatch.setattr(push.ApnsConfig, "from_env", staticmethod(fake_from_env))
        monkeypatch.setattr(push, "ApnsClient", lambda _cfg: fake)

        n1 = _run(push.dispatch(
            store_with_one_device, FakeUser("userA"), FakeListing("l1"),
        ))
        assert n1 == 0

        # 模拟 60s 重试窗口已过；下一次真实通知应重新读配置并恢复发送。
        push._client_retry_after = 0.0  # noqa: SLF001 - intentional white-box test
        n2 = _run(push.dispatch(
            store_with_one_device, FakeUser("userA"), FakeListing("l2"),
        ))
        assert n2 == 1
        assert calls == 2


# ── 单条 dispatch ──────────────────────────────────────────────────


class TestDispatch:
    def test_new_listing_success(self, with_apns, store_with_one_device):
        n = _run(push.dispatch(
            store_with_one_device, FakeUser("userA"), FakeListing("l1"),
        ))
        assert n == 1
        call = with_apns.calls[0]
        assert call["payload"]["kind"] == "new"
        assert call["payload"]["listing_id"] == "l1"
        assert call["payload"]["aps"]["alert"]["title"].startswith("[H2S]")

    def test_booked(self, with_apns, store_with_one_device):
        n = _run(push.dispatch(
            store_with_one_device, FakeUser("userA"),
            FakeListing("l1"), kind="booked",
        ))
        assert n == 1
        assert with_apns.calls[0]["payload"]["kind"] == "booked"

    def test_no_devices_returns_0(self, with_apns):
        store = FakeStore({})
        n = _run(push.dispatch(store, FakeUser("userZ"), FakeListing("l1")))
        assert n == 0
        assert with_apns.calls == []

    def test_unknown_kind_fallback(self, with_apns, store_with_one_device):
        n = _run(push.dispatch(
            store_with_one_device, FakeUser("userA"),
            FakeListing("l1"), kind="weird",
        ))
        assert n == 0


# ── status_change ───────────────────────────────────────────────────


class TestStatusChange:
    def test_dispatch_status_change(self, with_apns, store_with_one_device):
        n = _run(push.dispatch_status_change(
            store_with_one_device, FakeUser("userA"),
            FakeListing("l1"), "lottery", "available",
        ))
        assert n == 1
        p = with_apns.calls[0]["payload"]
        assert p["kind"] == "status_change"
        assert "lottery" in p["aps"]["alert"]["body"]
        assert "available" in p["aps"]["alert"]["body"]


# ── 聚合 ────────────────────────────────────────────────────────────


class TestAggregate:
    def test_dispatch_aggregate(self, with_apns, store_with_one_device):
        listings = [FakeListing(f"l{i}", city="Eindhoven") for i in range(3)]
        n = _run(push.dispatch_aggregate(
            store_with_one_device, FakeUser("userA"), listings,
            round_id="2026-05-15T08:00:00Z",
        ))
        assert n == 1
        call = with_apns.calls[0]
        assert call["collapse_id"] == "2026-05-15T08:00:00Z"
        assert call["payload"]["kind"] == "round"
        assert "3" in call["payload"]["aps"]["alert"]["title"]

    def test_should_aggregate_threshold(self):
        assert push.should_aggregate(0) is False
        assert push.should_aggregate(2) is False
        assert push.should_aggregate(push.aggregate_threshold()) is True
        assert push.should_aggregate(100) is True


# ── 错误推送 ────────────────────────────────────────────────────────


class TestError:
    def test_dispatch_error(self, with_apns, store_with_one_device):
        n = _run(push.dispatch_error(
            store_with_one_device, FakeUser("userA"),
            "Cloudflare 403 屏蔽中",
        ))
        assert n == 1
        p = with_apns.calls[0]["payload"]
        assert p["kind"] == "blocked"
        assert "Cloudflare" in p["aps"]["alert"]["body"]


# ── 节流 ────────────────────────────────────────────────────────────


class TestDedup:
    def test_same_listing_dedup_within_window(self, with_apns, store_with_one_device):
        u = FakeUser("userA")
        n1 = _run(push.dispatch(store_with_one_device, u, FakeListing("dup")))
        n2 = _run(push.dispatch(store_with_one_device, u, FakeListing("dup")))
        assert n1 == 1
        assert n2 == 0   # 5 分钟内同 listing/kind 拒绝
        assert len(with_apns.calls) == 1

    def test_different_kind_not_deduped(self, with_apns, store_with_one_device):
        u = FakeUser("userA")
        _run(push.dispatch(store_with_one_device, u, FakeListing("l1"), kind="new"))
        _run(push.dispatch(store_with_one_device, u, FakeListing("l1"), kind="booked"))
        assert len(with_apns.calls) == 2

    def test_booking_failed_kind_is_dispatched(self, with_apns, store_with_one_device):
        """预订失败推送走真实 dispatch：认这个 kind、payload 对、豁免每用户限速。"""
        u = FakeUser("userA")
        for i in range(push._PER_USER_LIMIT):
            _run(push.dispatch(store_with_one_device, u, FakeListing(f"n{i}")))
        n = _run(push.dispatch(store_with_one_device, u, FakeListing("lf"),
                               kind="booking_failed"))
        assert n == 1
        assert with_apns.calls[-1]["payload"]["kind"] == "booking_failed"

    def test_different_listing_not_deduped(self, with_apns, store_with_one_device):
        u = FakeUser("userA")
        _run(push.dispatch(store_with_one_device, u, FakeListing("a")))
        _run(push.dispatch(store_with_one_device, u, FakeListing("b")))
        assert len(with_apns.calls) == 2

    def test_per_user_rate_limit(self, with_apns, store_with_one_device):
        """1 分钟内 ≤10 条；第 11 条起被节流。"""
        u = FakeUser("userA")
        for i in range(15):
            _run(push.dispatch(
                store_with_one_device, u, FakeListing(f"l{i}"),
            ))
        assert len(with_apns.calls) == 10


# ── disable_device 联动 ─────────────────────────────────────────────


class TestDisableOn410:
    def test_410_disables_device(self, monkeypatch):
        """ApnsResult device_dead=True 时 push.py 自动 disable。"""
        store = FakeStore({"userA": [
            {"id": 99, "device_token": "dead" + "0" * 60, "env": "production"},
        ]})
        fake = FakeApns(results_per_call=[[
            ApnsResult(status=410, reason="Unregistered",
                       device="dead" + "0" * 60),
        ]])
        push.set_client(fake)
        n = _run(push.dispatch(store, FakeUser("userA"), FakeListing("l1")))
        assert n == 0
        assert store.disabled == [(99, "Unregistered")]


# ── 异常吞掉 ────────────────────────────────────────────────────────


class TestExceptionsSwallowed:
    def test_storage_throws(self, with_apns):
        class BoomStore:
            def get_active_devices_for_user(self, _):
                raise RuntimeError("db down")
        n = _run(push.dispatch(BoomStore(), FakeUser("userA"),
                               FakeListing("l1")))
        assert n == 0

    def test_send_many_throws(self, store_with_one_device):
        class BoomApns:
            async def send_many(self, *a, **kw):
                raise RuntimeError("apns down")
        push.set_client(BoomApns())
        n = _run(push.dispatch(store_with_one_device, FakeUser("userA"),
                               FakeListing("l1")))
        assert n == 0


# ── Bilingual translation (_t / _T) ──────────────────────────────────


class TestTranslation:
    def test_t_en_returns_original(self):
        from mcore.push import _t
        assert _t("[{source}] {city} new listing", "en") == "[{source}] {city} new listing"

    def test_t_zh_returns_translated(self):
        from mcore.push import _t
        assert "新房源" in _t("[{source}] {city} new listing", "zh")

    def test_t_unknown_lang_falls_back_to_en(self):
        from mcore.push import _t
        assert _t("[{source}] {city} new listing", "fr") == "[{source}] {city} new listing"

    def test_t_unknown_key_returns_as_is(self):
        from mcore.push import _t
        assert _t("not in the translation map", "zh") == "not in the translation map"

    def test_payload_new_listing_zh_uses_chinese(self):
        from mcore.push import _payload_new_listing
        p = _payload_new_listing(FakeListing("l1"), lang="zh")
        assert "新房源" in p["aps"]["alert"]["title"]

    def test_payload_new_listing_en_uses_english(self):
        from mcore.push import _payload_new_listing
        p = _payload_new_listing(FakeListing("l1"), lang="en")
        assert "new listing" in p["aps"]["alert"]["title"]

    def test_payload_booked_zh(self):
        from mcore.push import _payload_booked
        p = _payload_booked(FakeListing("l1"), lang="zh")
        assert "预订成功" in p["aps"]["alert"]["title"]

    def test_payload_error_zh(self):
        from mcore.push import _payload_error
        p = _payload_error("something broke", lang="zh")
        assert "监控异常" in p["aps"]["alert"]["title"]


# ── Language-grouped payload construction ────────────────────────────


class TestLanguageGrouping:

    def test_payload_builder_invoked_per_language(self):
        """payload_fn is called with 'en' and 'zh' for mixed-language devices."""
        from mcore import push

        langs_seen = []

        def _track(lang):
            langs_seen.append(lang)
            return {"aps": {"alert": {"title": "", "body": ""}}, "kind": "test"}

        # Direct test of the grouping logic without full dispatch chain
        class FakeStorage:
            def get_active_devices_for_user(self, uid):
                return [
                    {"id": 1, "device_token": "a" * 32, "env": "production", "language": "en"},
                    {"id": 2, "device_token": "b" * 32, "env": "production", "language": "zh"},
                    {"id": 3, "device_token": "c" * 32, "env": "production", "language": "en"},
                ]
            conn = None  # not used by _send_to_user when devices exist

        class FakeApns:
            async def send_many(self, targets, payload, collapse_id=""):
                return [type("R", (), {"ok": True, "device_dead": False, "device": t["device_token"], "reason": "", "status": 200}) for t in targets]

        push.set_client(FakeApns())
        results = _run(push._send_to_user(FakeStorage(), "uid", _track))
        assert len(results) == 3
        assert set(langs_seen) == {"en", "zh"}

    def test_single_lang_one_call(self):
        """All devices same language → payload_fn called once."""
        from mcore import push

        langs_seen = []

        def _track(lang):
            langs_seen.append(lang)
            return {"aps": {"alert": {"title": "", "body": ""}}, "kind": "test"}

        class FakeStorage:
            def get_active_devices_for_user(self, uid):
                return [
                    {"id": 1, "device_token": "a" * 32, "env": "production", "language": "en"},
                    {"id": 2, "device_token": "b" * 32, "env": "production", "language": "en"},
                ]
            conn = None

        class FakeApns:
            async def send_many(self, targets, payload, collapse_id=""):
                return [type("R", (), {"ok": True, "device_dead": False, "device": t["device_token"], "reason": "", "status": 200}) for t in targets]

        push.set_client(FakeApns())
        results = _run(push._send_to_user(FakeStorage(), "uid", _track))
        assert len(results) == 2
        assert langs_seen == ["en"]


# ── FCM 调度（原 test_push_fcm.py） ─────────────────────────────────
#
# 覆盖：
# - FCM 未启用时 dispatch 全部 no-op（仅 APNs 路径不受影响）
# - FCM + APNs 双发分流（iOS → APNs, Android → FCM）
# - FCM device_dead → disable_device
# - FCM payload 构造（data-only, 不含 aps）
# - FCM 节流去重与 APNs 共享

class _FcmApns:
    def __init__(self):
        self.calls: list[dict] = []

    async def send_many(self, targets, *, payload, collapse_id="", **_):
        self.calls.append({"targets": targets, "payload": payload})
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


class _FcmStore:
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
class _FcmListing:
    id: str
    name: str = "test"
    city: str = "Eindhoven"
    status: str = "Available to book"
    price_display: str = "€700"
    available_from: str = "2026-06-01"

    def feature_map(self):
        return {"area": "26 m²"}


class TestPlatformSeparation:
    def test_ios_to_apns_only(self):
        """iOS 设备走 APNs，不碰 FCM。"""
        fake_apns = _FcmApns()
        fake_fcm = FakeFcm()
        push.set_client(fake_apns)
        push.set_fcm_client(fake_fcm)
        store = _FcmStore({"userA": [
            {"id": 1, "device_token": "ios_tok" + "0" * 57, "env": "production", "platform": "ios"},
        ]})

        n = _run(push.dispatch(store, type("U", (), {"id": "userA"})(), _FcmListing("l1")))
        assert n == 1
        assert len(fake_apns.calls) == 1
        assert len(fake_fcm.calls) == 0

    def test_android_to_fcm_only(self):
        """Android 设备走 FCM，不碰 APNs。"""
        fake_apns = _FcmApns()
        fake_fcm = FakeFcm()
        push.set_client(fake_apns)
        push.set_fcm_client(fake_fcm)
        store = _FcmStore({"userA": [
            {"id": 2, "device_token": "fcm_tok" * 8, "platform": "android", "language": "en"},
        ]})

        n = _run(push.dispatch(store, type("U", (), {"id": "userA"})(), _FcmListing("l1")))
        assert n == 1
        assert len(fake_apns.calls) == 0
        assert len(fake_fcm.calls) == 1

    def test_mixed_platforms_dual_dispatch(self):
        """混合设备同时走 APNs + FCM。"""
        fake_apns = _FcmApns()
        fake_fcm = FakeFcm()
        push.set_client(fake_apns)
        push.set_fcm_client(fake_fcm)
        store = _FcmStore({"userA": [
            {"id": 1, "device_token": "ios" + "0" * 61, "env": "production", "platform": "ios"},
            {"id": 2, "device_token": "fcm" * 22, "platform": "android", "language": "en"},
        ]})

        n = _run(push.dispatch(store, type("U", (), {"id": "userA"})(), _FcmListing("l1")))
        assert n == 2
        assert len(fake_apns.calls) == 1
        assert len(fake_fcm.calls) == 1

    def test_no_platform_defaults_to_apns(self):
        """未设 platform 的老设备默认走 APNs。"""
        fake_apns = _FcmApns()
        fake_fcm = FakeFcm()
        push.set_client(fake_apns)
        push.set_fcm_client(fake_fcm)
        store = _FcmStore({"userA": [
            {"id": 99, "device_token": "old" * 20, "env": "production"},  # no platform field
        ]})

        n = _run(push.dispatch(store, type("U", (), {"id": "userA"})(), _FcmListing("l1")))
        assert n == 1
        assert len(fake_apns.calls) == 1
        assert len(fake_fcm.calls) == 0

    def test_fcm_disabled_still_sends_apns(self):
        """FCM 未启用时 APNs 仍正常工作。"""
        fake_apns = _FcmApns()
        push.set_client(fake_apns)
        store = _FcmStore({"userA": [
            {"id": 1, "device_token": "ios" + "0" * 61, "env": "production", "platform": "ios"},
            {"id": 2, "device_token": "fcm" * 22, "platform": "android"},
        ]})

        n = _run(push.dispatch(store, type("U", (), {"id": "userA"})(), _FcmListing("l1")))
        assert n == 1  # only iOS
        assert len(fake_apns.calls) == 1


class TestFcmPayload:
    def test_new_listing_data_only(self):
        from mcore.push import _fcm_payload_new_listing
        p = _fcm_payload_new_listing(_FcmListing("l123"), lang="en")
        msg = p["message"]
        assert "notification" not in msg  # data-only
        assert msg["data"]["title"].startswith("[H2S]")
        assert msg["data"]["listing_id"] == "l123"
        assert msg["data"]["kind"] == "new"
        assert "h2smonitor" in msg["data"]["deep_link"]
        assert msg["android"]["priority"] == "high"

    def test_new_listing_zh(self):
        from mcore.push import _fcm_payload_new_listing
        p = _fcm_payload_new_listing(_FcmListing("l1"), lang="zh")
        assert "新房源" in p["message"]["data"]["title"]

    def test_status_change(self):
        from mcore.push import _fcm_payload_status_change
        p = _fcm_payload_status_change(_FcmListing("l2"), "lottery", "available", lang="en")
        data = p["message"]["data"]
        assert data["kind"] == "status_change"
        assert "lottery" in data["body"]
        assert "available" in data["body"]

    def test_booked(self):
        from mcore.push import _fcm_payload_booked
        p = _fcm_payload_booked(_FcmListing("l3"), lang="en")
        assert p["message"]["data"]["kind"] == "booked"

    def test_round_aggregate(self):
        from mcore.push import _fcm_payload_round_aggregate
        listings = [_FcmListing(f"l{i}") for i in range(3)]
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


class TestFcmDisableOnDeadToken:
    def test_unregistered_disables_device(self):
        store = _FcmStore({"userA": [
            {"id": 55, "device_token": "dead_tok" * 8, "platform": "android", "language": "en"},
        ]})
        fake_fcm = FakeFcm(results=[
            [FcmResult(status=404, reason="unregistered", device="dead_tok" * 8)],
        ])
        push.set_fcm_client(fake_fcm)
        # APNs must be present too for dispatch to proceed
        push.set_client(_FcmApns())

        n = _run(push.dispatch(store, type("U", (), {"id": "userA"})(), _FcmListing("l1")))
        assert n == 0  # FCM failed
        assert store.disabled == [(55, "unregistered")]


class TestFcmStatusChange:
    def test_fcm_status_change_dispatched(self):
        fake_fcm = FakeFcm()
        push.set_fcm_client(fake_fcm)
        push.set_client(_FcmApns())
        store = _FcmStore({"userA": [
            {"id": 3, "device_token": "fcm" * 22, "platform": "android", "language": "en"},
        ]})

        n = _run(push.dispatch_status_change(
            store, type("U", (), {"id": "userA"})(),
            _FcmListing("sc1"), "Available in lottery", "Available to book",
        ))
        assert n == 1
        payload = fake_fcm.calls[0]["payload"]
        assert payload["message"]["data"]["kind"] == "status_change"


class TestFcmAggregate:
    def test_fcm_aggregate_dispatched(self):
        fake_fcm = FakeFcm()
        push.set_fcm_client(fake_fcm)
        push.set_client(_FcmApns())
        store = _FcmStore({"userA": [
            {"id": 4, "device_token": "fcm" * 22, "platform": "android", "language": "en"},
        ]})

        listings = [_FcmListing(f"l{i}") for i in range(5)]
        n = _run(push.dispatch_aggregate(
            store, type("U", (), {"id": "userA"})(), listings,
            round_id="agg-1",
        ))
        assert n == 1
        call = fake_fcm.calls[0]
        assert call["collapse_key"] == "agg-1"
        assert call["payload"]["message"]["data"]["kind"] == "round"


class TestFcmError:
    def test_fcm_error_dispatched(self):
        fake_fcm = FakeFcm()
        push.set_fcm_client(fake_fcm)
        push.set_client(_FcmApns())
        store = _FcmStore({"userA": [
            {"id": 5, "device_token": "fcm" * 22, "platform": "android", "language": "en"},
        ]})

        n = _run(push.dispatch_error(
            store, type("U", (), {"id": "userA"})(), "403 blocked",
        ))
        assert n == 1
        payload = fake_fcm.calls[0]["payload"]
        assert payload["message"]["data"]["kind"] == "blocked"


class TestFcmExceptionIsolation:
    def test_fcm_throw_does_not_block_apns(self):
        """FCM 异常不影响 APNs 正常发送。"""
        fake_apns = _FcmApns()

        class BoomFcm:
            async def send_many(self, *a, **kw):
                raise RuntimeError("fcm down")

        push.set_client(fake_apns)
        push.set_fcm_client(BoomFcm())
        store = _FcmStore({"userA": [
            {"id": 1, "device_token": "ios" + "0" * 61, "env": "production", "platform": "ios"},
            {"id": 2, "device_token": "fcm" * 22, "platform": "android", "language": "en"},
        ]})

        n = _run(push.dispatch(store, type("U", (), {"id": "userA"})(), _FcmListing("l1")))
        assert n == 1  # only iOS succeeds
        assert len(fake_apns.calls) == 1


class TestFcmAdmin:
    def test_fcm_admin_dispatched(self):
        fake_fcm = FakeFcm()
        push.set_fcm_client(fake_fcm)
        push.set_client(_FcmApns())
        store = _FcmStore({"__admin__": [
            {"id": 9, "device_token": "fcm" * 22, "platform": "android", "language": "en"},
        ]})

        n = _run(push.dispatch_admin(store, "alert!"))
        assert n == 1
        call = fake_fcm.calls[0]
        assert call["payload"]["message"]["data"]["kind"] == "blocked"
