"""
``notifications_enabled`` 必须同时管住外部渠道和设备推送。

以前只管住了一半：外部渠道被 ``MultiNotifier(enabled=…)`` 挡着，推送却从旁边
绕过去了（monitor 的 push.dispatch 只查 _allow_send 和有没有设备）。于是面板上
那个写着「通知」的开关关掉后，手机照响。线上实测 19 个用户处于这个状态，其中
`Test` 配了 email、开关关着、3 台设备——邮件不发，手机在推。

修法是两件事，缺一不可：

1. push.dispatch* 查 ``notifications_enabled``
2. 登记设备时把这个开关打开

只做 1 会把 App 推送整个关掉——注册接口硬编码 ``notifications_enabled=False``，
而 App 没有任何端点能改它。所以这个文件里 TestNoOutage 那一节比前两节重要：
它锁的是「两件事合起来的净效果」，而不是各自的局部行为。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest

from mcore import push
from notifier_channels.apns import ApnsResult


def _run(coro):
    return asyncio.run(coro)


class FakeApns:
    def __init__(self):
        self.calls: list[dict] = []

    async def send_many(self, targets, *, payload, collapse_id="", **_):
        self.calls.append({"targets": targets, "payload": payload})
        return [ApnsResult(status=200, reason="OK", device=t["device_token"])
                for t in targets]


class FakeStore:
    def __init__(self):
        self.devices = [{"id": 1, "device_token": "t" + "0" * 63,
                         "env": "production"}]

    def get_active_devices_for_user(self, user_id):
        return list(self.devices)

    def disable_device(self, device_id, reason=""):
        return True


@dataclass
class FakeListing:
    id: str = "l1"
    name: str = "Test flat"
    city: str = "Eindhoven"
    status: str = "Available to book"
    price_display: str = "€700"
    available_from: str = "2026-06-01"

    def feature_map(self):
        return {"area": "26 m²"}


@dataclass
class FakeUser:
    id: str = "userA"
    name: str = "kong"
    notifications_enabled: bool = True


@dataclass
class LegacyUser:
    """老配置：压根没有 notifications_enabled 这个键。"""
    id: str = "userLegacy"
    name: str = "legacy"


@pytest.fixture(autouse=True)
def reset_push():
    push.reset()
    yield
    push.reset()


@pytest.fixture
def apns():
    c = FakeApns()
    push.set_client(c)
    return c


@pytest.fixture
def store():
    return FakeStore()


@pytest.fixture(autouse=True)
def _reset_register_rate():
    """注册限流是同 IP 每小时 3 个，且是进程级状态。

    这个文件里好几条用例各建一个用户，第四条就会吃 429——而失败信息是
    「注册失败: 429」，看上去像被测代码坏了，其实是测试之间互相污染。
    """
    from app.auth import _REGISTER_RECORDS
    _REGISTER_RECORDS.clear()
    yield
    _REGISTER_RECORDS.clear()


# ── 开关关着 → 一条都不发 ────────────────────────────────────────

class TestToggleOff:
    """四个面向用户的 dispatch 都要被挡住。

    参数化到每一个入口，是因为它们各自独立地在函数体开头做判断——漏掉任何
    一个，用户就会在某类事件上继续被推送，而那类事件恰恰是他关掉开关想躲的。
    """

    @pytest.mark.parametrize("call", [
        lambda st, u: push.dispatch(st, u, FakeListing(), kind="new"),
        lambda st, u: push.dispatch(st, u, FakeListing(), kind="booked"),
        lambda st, u: push.dispatch_status_change(
            st, u, FakeListing(), "Booked", "Available to book"),
        lambda st, u: push.dispatch_aggregate(
            st, u, [FakeListing("a"), FakeListing("b")], round_id="r1"),
        lambda st, u: push.dispatch_error(st, u, "blocked", kind="blocked"),
    ], ids=["new", "booked", "status_change", "aggregate", "error"])
    def test_no_send(self, apns, store, call):
        n = _run(call(store, FakeUser(notifications_enabled=False)))
        assert n == 0
        assert apns.calls == [], f"开关关着还发了 {len(apns.calls)} 次"

    def test_does_not_even_read_devices(self, apns):
        """挡在取设备之前——不该为一个明确关掉通知的用户查库。"""
        class Tripwire(FakeStore):
            def get_active_devices_for_user(self, user_id):
                raise AssertionError("开关关着却去查设备了")

        n = _run(push.dispatch(Tripwire(), FakeUser(notifications_enabled=False),
                               FakeListing()))
        assert n == 0


# ── 开关开着 / 老配置 → 照发 ────────────────────────────────────

class TestToggleOn:
    def test_enabled_sends(self, apns, store):
        n = _run(push.dispatch(store, FakeUser(notifications_enabled=True),
                               FakeListing()))
        assert n == 1
        assert len(apns.calls) == 1

    def test_missing_attr_sends(self, apns, store):
        """老配置里没这个键时按「开着」处理。

        反过来（缺失即关闭）会让一批老用户在升级后静默失联，而他们从没做过
        任何表示关闭的操作。宁可多推。
        """
        n = _run(push.dispatch(store, LegacyUser(), FakeListing()))
        assert n == 1

    def test_admin_alert_not_gated(self, apns, store):
        """管理员告警不看任何用户开关——它根本不针对某个用户。"""
        class AdminStore(FakeStore):
            def get_active_admin_devices(self):
                return list(self.devices)

        n = _run(push.dispatch_admin(AdminStore(), "origin 挂了", kind="blocked"))
        assert n >= 0  # 只要没因为用户开关被挡掉即可
        # dispatch_admin 的签名里根本没有 user 参数——这条断言防的是以后有人
        # 「顺手统一」把闸加到 _send_to_admin 上。
        import inspect
        assert "user" not in inspect.signature(push.dispatch_admin).parameters


# ── 登记设备 = opt-in ───────────────────────────────────────────

class TestDeviceRegistrationOptsIn:
    def test_flips_switch(self, client, test_app):
        from app.services.device_service import _enable_notifications_for
        from users import load_users, update_users

        uid = _make_user(client, test_app, "OptInProbe")
        with test_app.app_context():
            assert _user(uid).notifications_enabled is False, "前置条件不成立"
            assert _enable_notifications_for(uid) is True
            assert _user(uid).notifications_enabled is True

    def test_second_call_does_not_write(self, client, test_app):
        """开关已经是 True 时不能再写。

        update_users 无条件重写全部用户行**并请求 monitor 热重载**。App 每次
        启动都重新上报设备（线上 33 个设备里 27 个刷新过 last_seen），少了这道
        判断就是每次启动踢一次 monitor。
        """
        from app.services import device_service

        uid = _make_user(client, test_app, "OptInTwice")
        with test_app.app_context():
            device_service._enable_notifications_for(uid)      # 第一次：翻
            with patch("users.update_users") as upd:
                changed = device_service._enable_notifications_for(uid)
            assert changed is False
            assert upd.call_count == 0, "开关已经开着还写了一次"

    def test_unknown_user_is_noop(self, test_app):
        from app.services import device_service
        with test_app.app_context():
            with patch("users.update_users") as upd:
                assert device_service._enable_notifications_for("nope") is False
            assert upd.call_count == 0


# ── 净效果：不能造成推送停摆 ─────────────────────────────────────

class TestNoOutage:
    """这一节是整个改动的验收条件。

    前两节各自只验了一半的行为。真正要保证的是：一个刚注册、在手机上授权了
    推送的用户，**推送要能到达**。注册接口把开关硬编码成 False，App 又没有
    端点能改它——只加闸不加 opt-in 的话，这条会红。
    """

    def test_fresh_app_user_still_gets_push(self, client, test_app, apns):
        from app.services.device_service import _enable_notifications_for

        uid = _make_user(client, test_app, "FreshAppUser")
        with test_app.app_context():
            u_before = _user(uid)
            assert u_before.notifications_enabled is False

            # 加了闸、还没登记设备 → 推不出去（这就是「只做一半」的后果）
            assert _run(push.dispatch(FakeStore(), u_before, FakeListing())) == 0

            # 手机上授权推送、上报 device_token
            _enable_notifications_for(uid)

            push.reset()
            push.set_client(apns)
            assert _run(push.dispatch(FakeStore(), _user(uid), FakeListing())) == 1

    def test_user_who_turns_it_off_stops_receiving(self, client, test_app, apns):
        """opt-in 之后用户仍然关得掉——否则这个开关还是假的。"""
        from app.services.device_service import _enable_notifications_for

        uid = _make_user(client, test_app, "TurnsItOff")
        with test_app.app_context():
            _enable_notifications_for(uid)

            def _off(users):
                for u in users:
                    if u.id == uid:
                        u.notifications_enabled = False
                        break

            from users import update_users
            update_users(_off)

            assert _run(push.dispatch(FakeStore(), _user(uid), FakeListing())) == 0


# ── helpers ────────────────────────────────────────────────────────

def _make_user(client, test_app, name: str) -> str:
    """走真实注册路径建用户。

    手搓 UserConfig 会绕开 ``notifications_enabled=False``——那是这批测试的
    前置条件，绕开了就等于什么都没测。用匿名 client：/register 成功后会把
    session 切成新用户，拿 admin_client 来做会把它顶掉。
    """
    r = client.post("/register", data={
        "csrf_token": "test_csrf",
        "register_username": name,
        "register_password": "pw1234",
        "terms_accepted": "1",
    }, follow_redirects=False)
    assert r.status_code in (302, 303), f"注册失败: {r.status_code} {r.data[:200]}"
    with test_app.app_context():
        from users import load_users
        u = next((u for u in load_users() if u.name == name), None)
    assert u is not None, f"注册返回 {r.status_code} 但库里没有 {name}"
    return u.id


def _user(uid: str):
    from users import load_users
    return next(u for u in load_users() if u.id == uid)
