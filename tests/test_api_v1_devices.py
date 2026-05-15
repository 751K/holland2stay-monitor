"""
/api/v1/devices/* 路由测试
============================

覆盖：
- POST /devices/register 合法注册 / 缺字段 / 错 env / 短 token
- 同一 token 多次 register → 幂等
- GET  /devices 列表只返回自己会话的；不泄漏完整 device_token
- DELETE /devices/<id> 只能删自己的；他人 401/404
- Bearer 缺失统一 401
"""

from __future__ import annotations

import pytest

from config import ListingFilter
from users import UserConfig, save_users, set_app_password


@pytest.fixture
def api_app(test_app, tmp_path, monkeypatch):
    monkeypatch.setattr("app.db.DB_PATH", tmp_path / "dev_api.db")
    from app import api_auth
    api_auth.invalidate_token_cache()
    yield test_app
    api_auth.invalidate_token_cache()


@pytest.fixture
def api_client(api_app):
    return api_app.test_client()


@pytest.fixture
def two_users(api_app):
    """两个 user，便于跨会话测试。"""
    users = []
    for name, uid in [("kong", "kong0001"), ("alice", "alice002")]:
        u = UserConfig(name=name, id=uid, listing_filter=ListingFilter())
        u.app_login_enabled = True
        set_app_password(u, f"{name}_pw_xyz")
        users.append(u)
    save_users(users)
    return users


def _login(api_client, username, password):
    r = api_client.post("/api/v1/auth/login", json={
        "username": username, "password": password})
    return r.get_json()["data"]["token"]


def _bearer(tok):
    return {"Authorization": f"Bearer {tok}"}


# ── 鉴权 ────────────────────────────────────────────────────────────


class TestAuth:
    def test_anon_register(self, api_client, two_users):
        r = api_client.post("/api/v1/devices/register", json={})
        assert r.status_code == 401

    def test_anon_list(self, api_client, two_users):
        assert api_client.get("/api/v1/devices").status_code == 401

    def test_anon_delete(self, api_client, two_users):
        assert api_client.delete("/api/v1/devices/1").status_code == 401


# ── 注册 ────────────────────────────────────────────────────────────


class TestRegister:
    def test_success(self, api_client, two_users, test_credentials):
        tok = _login(api_client, "__admin__", test_credentials["password"])
        r = api_client.post(
            "/api/v1/devices/register",
            headers=_bearer(tok),
            json={
                "device_token": "abc123" + "0" * 58,
                "env": "production",
                "model": "iPhone15,2",
                "bundle_id": "com.x.y",
            },
        )
        assert r.status_code == 200
        d = r.get_json()["data"]
        assert d["device_id"] > 0
        assert d["env"] == "production"

    def test_idempotent_same_token(self, api_client, two_users):
        tok = _login(api_client, "kong", "kong_pw_xyz")
        r1 = api_client.post(
            "/api/v1/devices/register",
            headers=_bearer(tok),
            json={"device_token": "tok" + "0" * 61, "env": "production"},
        )
        r2 = api_client.post(
            "/api/v1/devices/register",
            headers=_bearer(tok),
            json={"device_token": "tok" + "0" * 61, "env": "sandbox"},
        )
        assert r1.get_json()["data"]["device_id"] == r2.get_json()["data"]["device_id"]
        # env 已经被刷新
        rr = api_client.get("/api/v1/devices", headers=_bearer(tok)).get_json()["data"]
        assert rr["items"][0]["env"] == "sandbox"

    def test_missing_token(self, api_client, two_users):
        tok = _login(api_client, "kong", "kong_pw_xyz")
        r = api_client.post(
            "/api/v1/devices/register",
            headers=_bearer(tok),
            json={},
        )
        assert r.status_code == 400
        assert r.get_json()["error"]["code"] == "validation"

    def test_bad_env(self, api_client, two_users):
        tok = _login(api_client, "kong", "kong_pw_xyz")
        r = api_client.post(
            "/api/v1/devices/register",
            headers=_bearer(tok),
            json={"device_token": "x" * 64, "env": "weird"},
        )
        assert r.status_code == 400

    def test_short_token(self, api_client, two_users):
        tok = _login(api_client, "kong", "kong_pw_xyz")
        r = api_client.post(
            "/api/v1/devices/register",
            headers=_bearer(tok),
            json={"device_token": "short", "env": "production"},
        )
        assert r.status_code == 400


# ── 列表 ────────────────────────────────────────────────────────────


class TestList:
    def test_list_session_devices_only(self, api_client, two_users):
        kong_tok = _login(api_client, "kong", "kong_pw_xyz")
        alice_tok = _login(api_client, "alice", "alice_pw_xyz")
        api_client.post(
            "/api/v1/devices/register", headers=_bearer(kong_tok),
            json={"device_token": "kong" + "0" * 60, "env": "production"})
        api_client.post(
            "/api/v1/devices/register", headers=_bearer(alice_tok),
            json={"device_token": "alic" + "0" * 60, "env": "production"})

        r = api_client.get("/api/v1/devices", headers=_bearer(kong_tok))
        d = r.get_json()["data"]
        assert len(d["items"]) == 1
        # alice 的设备不应被 kong 看到
        for item in d["items"]:
            assert "alic" not in item["device_token_hint"]

    def test_hint_no_full_token(self, api_client, two_users):
        tok = _login(api_client, "kong", "kong_pw_xyz")
        api_client.post(
            "/api/v1/devices/register", headers=_bearer(tok),
            json={"device_token": "secret_token_xxx" + "y" * 48,
                  "env": "production"})
        r = api_client.get("/api/v1/devices", headers=_bearer(tok))
        item = r.get_json()["data"]["items"][0]
        # token_hint 是前 12 + 末 4，不应是完整明文
        assert "secret_token_xxx" not in item["device_token_hint"] or "…" in item["device_token_hint"]


# ── 删除 ────────────────────────────────────────────────────────────


class TestDelete:
    def test_delete_own(self, api_client, two_users):
        tok = _login(api_client, "kong", "kong_pw_xyz")
        r = api_client.post(
            "/api/v1/devices/register", headers=_bearer(tok),
            json={"device_token": "d" * 64, "env": "production"})
        did = r.get_json()["data"]["device_id"]
        r2 = api_client.delete(
            f"/api/v1/devices/{did}", headers=_bearer(tok),
        )
        assert r2.status_code == 200
        # 二次删除 404（数据已物理删除）
        r3 = api_client.delete(
            f"/api/v1/devices/{did}", headers=_bearer(tok),
        )
        assert r3.status_code == 404

    def test_delete_other_session_404(self, api_client, two_users):
        kong_tok = _login(api_client, "kong", "kong_pw_xyz")
        alice_tok = _login(api_client, "alice", "alice_pw_xyz")
        r = api_client.post(
            "/api/v1/devices/register", headers=_bearer(kong_tok),
            json={"device_token": "k" * 64, "env": "production"})
        did = r.get_json()["data"]["device_id"]
        # alice 试图删 kong 的 → 404（不泄漏存在性）
        r2 = api_client.delete(
            f"/api/v1/devices/{did}", headers=_bearer(alice_tok),
        )
        assert r2.status_code == 404
        # kong 的设备仍存在
        kong_list = api_client.get(
            "/api/v1/devices", headers=_bearer(kong_tok),
        ).get_json()["data"]
        assert len(kong_list["items"]) == 1

    def test_delete_unknown(self, api_client, two_users):
        tok = _login(api_client, "kong", "kong_pw_xyz")
        r = api_client.delete(
            "/api/v1/devices/999999", headers=_bearer(tok),
        )
        assert r.status_code == 404
