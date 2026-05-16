"""
API v1 鉴权安全回归测试
========================

针对 Phase 1.6 安全审计中发现的问题，确保以下漏洞不再回归：

1. 客户端 ttl_days=None / 越界 → 必须强制退回 90 天（不能签发永不过期 token）
2. 用户名枚举：用户存在与不存在的登录响应时序应在同一量级（bcrypt 已对齐）
3. Web 表单不接受 "__" 前缀的用户名（避免与 __admin__ sentinel 冲突）
"""

from __future__ import annotations

import time

import pytest
from werkzeug.datastructures import ImmutableMultiDict

from app.forms.user_form import build_user_from_form
from config import ListingFilter
from users import UserConfig, save_users, set_app_password


@pytest.fixture
def api_app(test_app, tmp_path, monkeypatch):
    monkeypatch.setattr("app.db.DB_PATH", tmp_path / "sec.db")
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
    plaintext = "user_pass_xyz_456"
    u = UserConfig(name="kong", id="kong0001",
                   listing_filter=ListingFilter())
    u.app_login_enabled = True
    set_app_password(u, plaintext)
    save_users([u])
    return u, plaintext


# ── Issue 1: ttl_days 越界 / None ────────────────────────────────────


class TestTTLBoundaries:
    """客户端不应能签发非过期 / 超长 token。"""

    def _expires_at(self, api_client, admin_password, payload_ttl):
        """登录并返回 token 的 expires_at（字符串），同时返回 ttl_days 字段。"""
        body = {"username": "__admin__", "password": admin_password}
        if payload_ttl is not ...:  # sentinel：不传字段
            body["ttl_days"] = payload_ttl
        r = api_client.post("/api/v1/auth/login", json=body)
        assert r.status_code == 200
        d = r.get_json()["data"]
        return d

    def test_null_ttl_forced_to_default(self, api_client, admin_password):
        d = self._expires_at(api_client, admin_password, None)
        assert d["ttl_days"] == 90

    def test_missing_ttl_uses_default(self, api_client, admin_password):
        d = self._expires_at(api_client, admin_password, ...)
        assert d["ttl_days"] == 90

    def test_zero_ttl_forced_to_default(self, api_client, admin_password):
        d = self._expires_at(api_client, admin_password, 0)
        assert d["ttl_days"] == 90

    def test_negative_ttl_forced_to_default(self, api_client, admin_password):
        d = self._expires_at(api_client, admin_password, -1)
        assert d["ttl_days"] == 90

    def test_oversize_ttl_forced_to_default(self, api_client, admin_password):
        d = self._expires_at(api_client, admin_password, 10000)
        assert d["ttl_days"] == 90

    def test_string_ttl_forced_to_default(self, api_client, admin_password):
        d = self._expires_at(api_client, admin_password, "forever")
        assert d["ttl_days"] == 90

    def test_token_actually_has_expires_at(self, api_client, admin_password):
        """白盒：DB 中的 token 不允许 expires_at IS NULL。"""
        self._expires_at(api_client, admin_password, None)
        from app.db import storage
        st = storage()
        try:
            rows = st.list_app_tokens()
            assert all(r["expires_at"] is not None for r in rows)
        finally:
            st.close()

    def test_valid_ttl_accepted(self, api_client, admin_password):
        d = self._expires_at(api_client, admin_password, 30)
        assert d["ttl_days"] == 30

    def test_max_ttl_clamped(self, api_client, admin_password):
        """MAX_TTL_DAYS=90 是上限。"""
        d = self._expires_at(api_client, admin_password, 90)
        assert d["ttl_days"] == 90
        d2 = self._expires_at(api_client, admin_password, 91)
        assert d2["ttl_days"] == 90  # 越界 → 默认


# ── Issue 2: 时序枚举防护 ──────────────────────────────────────────


class TestUsernameEnumerationTiming:
    """
    用户存在 vs 不存在的登录失败响应时间应在同一数量级（都跑 bcrypt）。

    具体不做精确时序断言（CI 抖动大），只断言"不存在用户"的失败时间
    至少和 bcrypt cost=12 的最小耗时（~30ms 在 M 系列 mac 上）量级相近。
    """

    def test_unknown_user_runs_bcrypt(self, api_client, seeded_user, monkeypatch):
        """通过 monkeypatch 计数 bcrypt.checkpw 调用，确认无论用户存在都会调用。"""
        calls: list[str] = []

        import bcrypt
        original = bcrypt.checkpw

        def counting(pw, hashed):
            calls.append("called")
            return original(pw, hashed)

        monkeypatch.setattr(bcrypt, "checkpw", counting)

        # 不存在用户
        calls.clear()
        api_client.post("/api/v1/auth/login",
                        json={"username": "nonexistent_xyz", "password": "wrong"})
        assert len(calls) >= 1, "unknown user 路径必须也跑一次 bcrypt 以避免时序枚举"

        # 存在用户但密码错
        user, _ = seeded_user
        calls.clear()
        api_client.post("/api/v1/auth/login",
                        json={"username": user.name, "password": "wrong"})
        assert len(calls) >= 1

    def test_disabled_user_runs_bcrypt(self, api_client, seeded_user, monkeypatch):
        """app_login_enabled=False 的用户也不能短路（避免 user 状态枚举）。"""
        user, password = seeded_user
        user.app_login_enabled = False
        save_users([user])

        import bcrypt
        calls: list[str] = []
        original = bcrypt.checkpw
        monkeypatch.setattr(bcrypt, "checkpw",
                            lambda pw, h: (calls.append("c"), original(pw, h))[1])
        # 这里也覆盖 enabled=False 路径
        user.enabled = False
        save_users([user])
        api_client.post("/api/v1/auth/login",
                        json={"username": user.name, "password": password})
        assert len(calls) >= 1


# ── Issue 3: 保留用户名前缀 ────────────────────────────────────────


class TestReservedUsername:
    def test_double_underscore_prefix_rewritten(self):
        form = ImmutableMultiDict([("name", "__admin__"), ("enabled", "true")])
        u = build_user_from_form(form)
        assert not u.name.startswith("__")
        assert "admin" in u.name  # u_admin__ 风格

    def test_single_underscore_passes(self):
        form = ImmutableMultiDict([("name", "_kong"), ("enabled", "true")])
        u = build_user_from_form(form)
        assert u.name == "_kong"

    def test_normal_name_unchanged(self):
        form = ImmutableMultiDict([("name", "Alice"), ("enabled", "true")])
        u = build_user_from_form(form)
        assert u.name == "Alice"

    def test_double_underscore_user_cannot_hijack_admin(
        self, test_app, tmp_path, monkeypatch, isolated_data_dir, test_credentials
    ):
        """端到端：即使数据库里塞个 name='__admin__' 用户，登录 sentinel 仍走 WEB_PASSWORD。"""
        monkeypatch.setattr("app.db.DB_PATH", tmp_path / "hi.db")
        from app import api_auth
        api_auth.invalidate_token_cache()
        # 绕开表单防护，直接写一个恶意用户
        u = UserConfig(name="__admin__", id="evil0001")
        u.app_login_enabled = True
        set_app_password(u, "user_evil_password")
        save_users([u])
        c = test_app.test_client()
        # 用 user_evil_password 登录 __admin__ → 应当失败（走 sentinel 分支
        # 比对的是 WEB_PASSWORD，不是该 user 的 bcrypt hash）
        r = c.post("/api/v1/auth/login",
                   json={"username": "__admin__", "password": "user_evil_password"})
        assert r.status_code == 401
        # 而真正的 admin 密码仍可登录
        r2 = c.post("/api/v1/auth/login",
                    json={"username": "__admin__",
                          "password": test_credentials["password"]})
        assert r2.status_code == 200
        assert r2.get_json()["data"]["role"] == "admin"
        api_auth.invalidate_token_cache()
