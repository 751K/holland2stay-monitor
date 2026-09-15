"""推送按平台分流：规则只在 ``device_service.push_channel`` 一处，白名单。

起因（2026-09-15，为 macOS 客户端做准备）
----------------------------------------
分流原来写在四个地方，写法各不相同：

- ``/devices/test``（device_service）：``in ("ios",)`` → macOS 设备两边都不走
- ``mcore/push.py`` 用户推送 / admin 推送：``!= "android"`` → 黑名单，拼错的字符串也送 APNs
- 管理员测试推送（app_accounts）：``!= "android"``

而且注册时不校验 ``platform``，传什么存什么。没有任何测试检查按平台分流，四处
写得不一致也一直是绿的。
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from app.services.device_service import (
    APNS_PLATFORMS,
    FCM_PLATFORMS,
    VALID_PLATFORMS,
    push_channel,
)
from mcore import push
from tests.test_api_v1_devices import _bearer, _login, api_app, api_client, two_users  # noqa: F401
from tests.test_app_accounts_routes import admin, isolated_db, issued_tokens  # noqa: F401
from tests.test_push_fcm import FakeApns, FakeFcm, FakeListing, FakeStore

_ROOT = Path(__file__).resolve().parent.parent


# ── 规则本身 ─────────────────────────────────────────────────────────

class TestPushChannel:
    @pytest.mark.parametrize("platform,want", [
        ("ios", "apns"), ("macos", "apns"), ("android", "fcm"),
        ("MacOS", "apns"), (" android ", "fcm"),
        (None, "apns"), ("", "apns"),          # 老数据 / iOS 客户端不发这个字段
        ("windows", None), ("iphone", None),   # 白名单：不认识的不推
    ])
    def test_table(self, platform, want):
        assert push_channel(platform) == want

    def test_every_valid_platform_has_a_channel(self):
        """能注册却没有通道 = 注册成功、永远收不到推送。"""
        assert {p for p in VALID_PLATFORMS if push_channel(p) is None} == set()

    def test_channels_are_disjoint_and_registrable(self):
        assert not (APNS_PLATFORMS & FCM_PLATFORMS)
        assert APNS_PLATFORMS | FCM_PLATFORMS == VALID_PLATFORMS


def test_no_call_site_compares_platform_strings_by_hand():
    """四处都得调 push_channel。手写的比较正是这次不一致的来源。"""
    sites = {
        "app/services/device_service.py": 2,
        "app/routes/app_accounts.py": 2,
        "mcore/push.py": 1,   # _devices_for 内部调一次，四个调用点都走它
    }
    for rel, min_calls in sites.items():
        src = (_ROOT / rel).read_text(encoding="utf-8")
        hand = re.findall(r"""get\(\s*["']platform["'][^)]*\)\s*(?:==|!=|in\b)""", src)
        assert not hand, f"{rel} 又手写了平台比较: {hand}"
        assert src.count("push_channel(") >= min_calls, f"{rel} 没走 push_channel"


# ── 注册 ─────────────────────────────────────────────────────────────

class TestRegisterValidatesPlatform:
    def _register(self, api_client, platform, token_suffix="0"):
        tok = _login(api_client, "kong", "kong_pw_xyz")
        body = {"device_token": "mac" + token_suffix * 61, "env": "production"}
        if platform is not ...:
            body["platform"] = platform
        return api_client.post("/api/v1/devices/register", headers=_bearer(tok), json=body)

    def test_macos_is_accepted(self, api_client, two_users):
        r = self._register(api_client, "macos")
        assert r.status_code == 200
        assert r.get_json()["data"]["platform"] == "macos"

    def test_unknown_platform_is_400(self, api_client, two_users):
        """不校验的话「windows」会被存下来，然后按分流规则永远不推。"""
        r = self._register(api_client, "windows")
        assert r.status_code == 400
        assert "platform" in r.get_json()["error"]["message"]

    def test_missing_platform_still_means_ios(self, api_client, two_users):
        """现有 iOS 客户端根本不发这个字段——不能因为加了校验就把它挡在外面。"""
        r = self._register(api_client, ..., token_suffix="1")
        assert r.status_code == 200
        assert r.get_json()["data"]["platform"] == "ios"


# ── /devices/test ────────────────────────────────────────────────────

class TestDeviceTestPushRoutesMacosToApns:
    def test_macos_device_goes_to_apns(self, api_client, two_users, monkeypatch):
        """原来这里是 ``in ("ios",)``：macOS 设备 APNs、FCM 两边都不走，测试推送 sent=0。"""
        import notifier_channels.apns as apns_mod
        import notifier_channels.fcm as fcm_mod

        sent_targets: list = []

        class _FakeClient:
            def __init__(self, cfg):
                pass

            async def send_many(self, targets, *, payload, **_):
                sent_targets.extend(targets)
                return [apns_mod.ApnsResult(status=200, reason="OK", device=t["device_token"])
                        for t in targets]

            async def close(self):
                pass

        monkeypatch.setattr(apns_mod.ApnsConfig, "from_env", classmethod(lambda cls: object()))
        monkeypatch.setattr(apns_mod, "ApnsClient", _FakeClient)
        monkeypatch.setattr(fcm_mod.FcmConfig, "from_env",
                            classmethod(lambda cls: pytest.fail("macOS 设备不该碰 FCM")))

        tok = _login(api_client, "kong", "kong_pw_xyz")
        mac_token = "mac" + "7" * 61
        r = api_client.post("/api/v1/devices/register", headers=_bearer(tok),
                            json={"device_token": mac_token, "platform": "macos"})
        assert r.status_code == 200

        r = api_client.post("/api/v1/devices/test", headers=_bearer(tok), json={"apns_only": True})
        assert r.status_code == 200, r.get_json()
        data = r.get_json()["data"]
        assert [t["device_token"] for t in sent_targets] == [mac_token]
        assert data["sent"] == 1
        assert [d["platform"] for d in data["results"]] == ["macos"]


# ── dispatch ─────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_push():
    push.reset()
    yield
    push.reset()


def _user(uid="userA"):
    return type("U", (), {"id": uid})()


class TestDispatchRoutesMacos:
    def _setup(self, devices, key="userA"):
        apns, fcm = FakeApns(), FakeFcm()
        push.set_client(apns)
        push.set_fcm_client(fcm)
        return apns, fcm, FakeStore({key: devices})

    def test_dispatch_pushes_to_macos(self):
        apns, fcm, store = self._setup([
            {"id": 7, "device_token": "mac" + "0" * 61, "env": "production", "platform": "macos"},
        ])
        n = asyncio.run(push.dispatch(store, _user(), FakeListing("l1")))
        assert n == 1
        assert [t["device_token"] for t in apns.calls[0]["targets"]] == ["mac" + "0" * 61]
        assert fcm.calls == []

    def test_unknown_platform_is_not_pushed_anywhere(self):
        """白名单：原来 ``!= "android"`` 会把它送去 APNs。"""
        apns, fcm, store = self._setup([
            {"id": 8, "device_token": "win" + "0" * 61, "env": "production", "platform": "windows"},
        ])
        n = asyncio.run(push.dispatch(store, _user(), FakeListing("l2")))
        assert n == 0
        assert apns.calls == [] and fcm.calls == []

    def test_admin_push_reaches_macos(self):
        apns, fcm, store = self._setup([
            {"id": 9, "device_token": "adm" + "0" * 61, "env": "production", "platform": "macos"},
        ], key="__admin__")
        asyncio.run(push._send_to_admin(store, lambda lang: {"aps": {"alert": "x"}}))
        assert len(apns.calls) == 1 and fcm.calls == []


# ── 管理面板 ─────────────────────────────────────────────────────────


def test_admin_devices_tab_shows_macos_as_apple(admin, issued_tokens):
    """原来只有 ``== 'ios'`` 才显示苹果图标，macOS 设备是通用手机图标加灰色 macos 标签。"""
    (aid, _), _ = issued_tokens
    from app.db import storage
    st = storage()
    try:
        st.register_device(app_token_id=aid, device_token="mac" + "5" * 61,
                           env="production", platform="macos")
    finally:
        st.close()

    html = admin.get("/settings/app-accounts?tab=devices").get_data(as_text=True)
    token = "mac" + "5" * 61
    at = html.find(f'title="{token}"')
    assert at != -1, "设备表里找不到这台 macOS 设备"
    row = html[html.rfind("<tr", 0, at): html.find("</tr>", at)]
    assert "bi-apple" in row
    assert ">macOS</span>" in row
    assert "badge-neutral" not in row
