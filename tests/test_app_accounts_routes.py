"""
/settings/app-accounts 路由测试
================================

覆盖：
- guest/anon 访问 → 重定向/拒绝
- admin GET 空列表
- admin GET 含已签发 token，user_name 已解析
- show_revoked 切换
- POST revoke 成功 / 不存在 / 二次撤销
- 撤销后 cache 被清空（影响 API v1 鉴权）
- 撤销路由要求 CSRF
"""

from __future__ import annotations

import pytest

from config import ListingFilter
from users import UserConfig, save_users


@pytest.fixture
def isolated_db(test_app, tmp_path, monkeypatch):
    """tmp DB 隔离，避免 token 表污染真实库。"""
    monkeypatch.setattr("app.db.DB_PATH", tmp_path / "appacc.db")
    from app import api_auth
    api_auth.invalidate_token_cache()
    yield tmp_path
    api_auth.invalidate_token_cache()


@pytest.fixture
def admin(test_app, isolated_db):
    c = test_app.test_client()
    with c.session_transaction() as s:
        s["authenticated"] = True
        s["role"] = "admin"
        s["csrf_token"] = "test_csrf"
    return c


@pytest.fixture
def issued_tokens(isolated_db):
    """签发一个 admin token + 一个 user token。返回 ((aid, atok), (uid, utok))."""
    # 先准备一个用户
    u = UserConfig(name="kong", id="kong0001",
                   listing_filter=ListingFilter())
    save_users([u])
    from app.db import storage
    st = storage()
    try:
        aid, atok = st.create_app_token(
            role="admin", user_id=None, device_name="iPhone-Admin")
        uid, utok = st.create_app_token(
            role="user", user_id="kong0001", device_name="iPhone-Kong")
    finally:
        st.close()
    return (aid, atok), (uid, utok)


# ── 鉴权 ────────────────────────────────────────────────────────────


class TestAuth:
    def test_anon_redirects(self, test_app):
        c = test_app.test_client()
        r = c.get("/settings/app-accounts")
        assert r.status_code == 302
        assert "/login" in r.headers["Location"]

    def test_guest_redirects(self, test_app, isolated_db):
        c = test_app.test_client()
        with c.session_transaction() as s:
            s["authenticated"] = True
            s["role"] = "guest"
        # admin_required 对 guest 重定向到首页
        r = c.get("/settings/app-accounts")
        assert r.status_code == 302


# ── GET ────────────────────────────────────────────────────────────


class TestList:
    def test_empty(self, admin):
        r = admin.get("/settings/app-accounts")
        assert r.status_code == 200
        # 模板的"暂无活跃会话"出现
        assert "app_accounts_empty" not in r.get_data(as_text=True)  # 翻译过的中文/英文
        # 至少 placeholder 在页面里
        assert b"app-accounts" in r.data or b"App" in r.data

    def test_lists_active_tokens(self, admin, issued_tokens):
        r = admin.get("/settings/app-accounts")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        # 两条 token 的设备名都应出现
        assert "iPhone-Admin" in body
        assert "iPhone-Kong" in body
        # user 关联的用户名应被解析为 "kong"
        assert "kong" in body

    def test_show_revoked_toggle(self, admin, issued_tokens, isolated_db):
        # 撤销 admin token
        (aid, _), _ = issued_tokens
        from app.db import storage
        st = storage(); st.revoke_app_token(aid); st.close()

        # 默认隐藏：admin 设备不应出现
        r = admin.get("/settings/app-accounts")
        body = r.get_data(as_text=True)
        assert "iPhone-Admin" not in body
        assert "iPhone-Kong" in body

        # show_revoked=1：两条都应出现
        r2 = admin.get("/settings/app-accounts?show_revoked=1")
        body2 = r2.get_data(as_text=True)
        assert "iPhone-Admin" in body2
        assert "iPhone-Kong" in body2


# ── POST revoke ────────────────────────────────────────────────────


class TestRevoke:
    def test_revoke_active(self, admin, issued_tokens):
        (aid, _), _ = issued_tokens
        r = admin.post(
            f"/settings/app-accounts/{aid}/revoke",
            data={"csrf_token": "test_csrf"},
        )
        assert r.status_code == 302
        # 现在该 token 应已撤销
        from app.db import storage
        st = storage()
        try:
            rows = st.list_app_tokens(include_revoked=True)
            row = next(r for r in rows if r["id"] == aid)
            assert row["revoked"] == 1
        finally:
            st.close()

    def test_revoke_unknown(self, admin):
        r = admin.post(
            "/settings/app-accounts/999999/revoke",
            data={"csrf_token": "test_csrf"},
        )
        assert r.status_code == 302  # 重定向 + flash warning

    def test_revoke_already_revoked(self, admin, issued_tokens, isolated_db):
        (aid, _), _ = issued_tokens
        from app.db import storage
        st = storage(); st.revoke_app_token(aid); st.close()
        r = admin.post(
            f"/settings/app-accounts/{aid}/revoke",
            data={"csrf_token": "test_csrf"},
        )
        assert r.status_code == 302

    def test_revoke_requires_csrf(self, admin, issued_tokens):
        (aid, _), _ = issued_tokens
        r = admin.post(f"/settings/app-accounts/{aid}/revoke")
        assert r.status_code == 403

    def test_revoke_invalidates_cache(self, admin, issued_tokens, test_app):
        """撤销后用同一 token 调 API v1 应当立即 401（cache 已清）。"""
        (_, atok), _ = issued_tokens
        c2 = test_app.test_client()
        # 先验证 token 可用
        r = c2.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {atok}"})
        assert r.status_code == 200
        # admin 撤销
        # 找出 token_id：从 fixtures 已知是 aid
        (aid, _), _ = issued_tokens
        admin.post(
            f"/settings/app-accounts/{aid}/revoke",
            data={"csrf_token": "test_csrf"},
        )
        # 同一 token 现在应失效
        r2 = c2.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {atok}"})
        assert r2.status_code == 401
