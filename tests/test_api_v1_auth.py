"""
API v1 鉴权 HTTP 测试
======================

覆盖：
- /auth/login 三档身份
  · admin 用 __admin__ + WEB_PASSWORD 登录 → 签发 admin token
  · user 用 username + bcrypt password 登录 → 签发 user token
  · 错凭据 / 缺字段 / 用户停用 / app_login_enabled=False
- /auth/me 返回当前身份摘要（admin/user）
- /auth/logout 撤销 token
- Bearer 缺失 / 错 / 撤销后无效
- 数据隔离：role=user 时 user_id 必匹配 UserConfig.id

实现要点
--------
- 用 isolated_data_dir + 单独的 monkeypatch DB_PATH 把所有写盘隔离到 tmp_path
- bcrypt 真实运行（这本身就是要测试的依赖）
"""

from __future__ import annotations

import pytest

from config import ListingFilter
from mstorage._tokens import hash_token
from users import UserConfig, save_users, set_app_password


# ── 共享 fixture ───────────────────────────────────────────────────


@pytest.fixture
def api_app(test_app, tmp_path, monkeypatch):
    """
    test_app + tmp DB（隔离 token 表 / users.json 写入）。

    test_app 已经隔离了 users.json / .env / logs；这里再把 DB_PATH 重定向到
    tmp_path 下的 fresh db，避免 token 表污染真实库。
    """
    fake_db = tmp_path / "api_test.db"
    monkeypatch.setattr("app.db.DB_PATH", fake_db)
    # api_auth TTL 缓存可能在前置测试里有残留，清一下确保隔离
    from app import api_auth
    api_auth.invalidate_token_cache()
    yield test_app
    api_auth.invalidate_token_cache()


@pytest.fixture
def api_client(api_app):
    return api_app.test_client()


@pytest.fixture
def admin_password(test_credentials):
    return test_credentials["password"]


@pytest.fixture
def seeded_user(api_app):
    """在 users.json 写一个开 app_login_enabled 的用户。返回 (UserConfig, plaintext)."""
    plaintext = "user_pass_xyz_456"
    u = UserConfig(
        name="kong",
        id="abcd1234",
        enabled=True,
        listing_filter=ListingFilter(max_rent=900),
    )
    u.app_login_enabled = True
    set_app_password(u, plaintext)
    save_users([u])
    return u, plaintext


def _login(client, username, password, device="dev-1"):
    return client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password, "device_name": device},
    )


# ── /auth/login: admin ─────────────────────────────────────────────


class TestAdminLogin:
    def test_admin_login_success(self, api_client, admin_password):
        r = _login(api_client, "__admin__", admin_password)
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is True
        d = body["data"]
        assert d["role"] == "admin"
        assert d["user_id"] is None
        assert d["device_name"] == "dev-1"
        assert isinstance(d["token"], str) and len(d["token"]) >= 30

    def test_admin_wrong_password(self, api_client):
        r = _login(api_client, "__admin__", "wrong")
        assert r.status_code == 401
        assert r.get_json()["error"]["code"] == "unauthorized"

    def test_admin_login_when_no_password_configured(self, api_client, monkeypatch):
        monkeypatch.delenv("WEB_PASSWORD", raising=False)
        r = _login(api_client, "__admin__", "anything")
        assert r.status_code == 401


# ── /auth/login: user ──────────────────────────────────────────────


class TestUserLogin:
    def test_user_login_success(self, api_client, seeded_user):
        user, password = seeded_user
        r = _login(api_client, user.name, password)
        assert r.status_code == 200
        d = r.get_json()["data"]
        assert d["role"] == "user"
        assert d["user_id"] == user.id

    def test_user_wrong_password(self, api_client, seeded_user):
        user, _ = seeded_user
        r = _login(api_client, user.name, "wrong")
        assert r.status_code == 401

    def test_unknown_user(self, api_client, seeded_user):
        r = _login(api_client, "nonexistent", "anything")
        assert r.status_code == 401

    def test_disabled_user(self, api_client, seeded_user):
        user, password = seeded_user
        user.enabled = False
        save_users([user])
        r = _login(api_client, user.name, password)
        # enabled=False 走 403（forbidden），与 unauthorized 区分
        assert r.status_code == 403

    def test_app_login_disabled(self, api_client, seeded_user):
        """app_login_enabled=False 即使密码对也拒绝。"""
        user, password = seeded_user
        user.app_login_enabled = False
        save_users([user])
        r = _login(api_client, user.name, password)
        assert r.status_code == 401

    def test_user_without_password_hash(self, api_client):
        """新创建的用户没设密码 → 不能登录。"""
        u = UserConfig(name="noPw", id="11111111")
        u.app_login_enabled = True
        # 故意不调 set_app_password
        save_users([u])
        r = _login(api_client, "noPw", "anything")
        assert r.status_code == 401


# ── /auth/login: 参数校验 ───────────────────────────────────────────


class TestLoginValidation:
    def test_missing_username(self, api_client):
        r = api_client.post("/api/v1/auth/login", json={"password": "x"})
        assert r.status_code == 400
        assert r.get_json()["error"]["code"] == "validation"

    def test_missing_password(self, api_client):
        r = api_client.post("/api/v1/auth/login", json={"username": "x"})
        assert r.status_code == 400

    def test_empty_body(self, api_client):
        r = api_client.post("/api/v1/auth/login", json={})
        assert r.status_code == 400

    def test_no_body(self, api_client):
        r = api_client.post("/api/v1/auth/login")
        assert r.status_code == 400

    def test_device_name_truncated(self, api_client, admin_password):
        long_name = "x" * 200
        r = api_client.post("/api/v1/auth/login", json={
            "username": "__admin__", "password": admin_password,
            "device_name": long_name,
        })
        assert r.status_code == 200
        assert len(r.get_json()["data"]["device_name"]) <= 64


# ── /auth/me ───────────────────────────────────────────────────────


class TestMe:
    def test_me_admin(self, api_client, admin_password):
        token = _login(api_client, "__admin__", admin_password).get_json()["data"]["token"]
        r = api_client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        d = r.get_json()["data"]
        assert d["role"] == "admin"
        assert d["user_id"] is None
        assert d["user"] is None

    def test_me_user(self, api_client, seeded_user):
        user, password = seeded_user
        token = _login(api_client, user.name, password).get_json()["data"]["token"]
        r = api_client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        d = r.get_json()["data"]
        assert d["role"] == "user"
        assert d["user_id"] == user.id
        assert d["user"]["name"] == user.name
        # 敏感字段不应泄漏
        assert "email_password" not in d["user"]
        assert "telegram_token" not in d["user"]
        assert "app_password_hash" not in d["user"]
        # listing_filter 是 dict（已转换）
        assert d["user"]["listing_filter"]["max_rent"] == 900

    def test_me_without_token(self, api_client):
        r = api_client.get("/api/v1/auth/me")
        assert r.status_code == 401

    def test_me_with_bad_token(self, api_client):
        r = api_client.get(
            "/api/v1/auth/me",
            headers={"Authorization": "Bearer not_a_real_token"},
        )
        assert r.status_code == 401

    def test_me_missing_bearer_prefix(self, api_client, admin_password):
        token = _login(api_client, "__admin__", admin_password).get_json()["data"]["token"]
        # 头部没有 "Bearer " 前缀
        r = api_client.get(
            "/api/v1/auth/me",
            headers={"Authorization": token},
        )
        assert r.status_code == 401

    def test_me_user_then_deleted(self, api_client, seeded_user):
        """登录后用户被删 → me 应返回 401（user 已不存在）。"""
        user, password = seeded_user
        token = _login(api_client, user.name, password).get_json()["data"]["token"]
        save_users([])
        # TTL 缓存里还有 token；token 本身没失效，但 me 读 users 拿不到
        r = api_client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 401


# ── /auth/logout ───────────────────────────────────────────────────


class TestLogout:
    def test_logout_revokes_token(self, api_client, admin_password):
        token = _login(api_client, "__admin__", admin_password).get_json()["data"]["token"]
        # 撤销
        r = api_client.post(
            "/api/v1/auth/logout",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        assert r.get_json()["data"]["revoked"] is True
        # 此后 me 必失败（cache 已 invalidate）
        r2 = api_client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r2.status_code == 401

    def test_logout_without_token(self, api_client):
        r = api_client.post("/api/v1/auth/logout")
        assert r.status_code == 401
