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
        assert call["payload"]["aps"]["alert"]["title"].startswith("🏠")

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
