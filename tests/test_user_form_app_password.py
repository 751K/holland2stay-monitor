"""
build_user_from_form 对 app_login / app_password 的处理。

三种语义：
  - 留空且未勾 clear → 保留旧 hash
  - 填新密码         → bcrypt 重新哈希
  - 勾 clear         → hash=""

同时验证 app_login_enabled 复选框是否被正确读取。
"""

from __future__ import annotations

import pytest
from werkzeug.datastructures import ImmutableMultiDict

from app.forms.user_form import build_user_from_form
from users import UserConfig, set_app_password, verify_app_password


def _form(**fields) -> ImmutableMultiDict:
    """便利：把 kwargs 转成 form 多字段映射。"""
    return ImmutableMultiDict([(k, v) for k, v in fields.items()])


def _existing_with_password() -> UserConfig:
    u = UserConfig(name="kong", id="kong0001")
    u.app_login_enabled = True
    set_app_password(u, "old_pw_xyz")
    return u


class TestAppPasswordSemantics:
    def test_new_user_with_password(self):
        form = _form(name="kong", app_login_enabled="true", app_password="brand_new_pw")
        u = build_user_from_form(form)
        assert u.app_login_enabled is True
        assert u.app_password_hash != ""
        assert verify_app_password(u, "brand_new_pw") is True

    def test_new_user_without_password(self):
        form = _form(name="x", app_login_enabled="true")
        u = build_user_from_form(form)
        assert u.app_login_enabled is True
        assert u.app_password_hash == ""

    def test_empty_password_keeps_existing(self):
        old = _existing_with_password()
        form = _form(name="kong", app_login_enabled="true")  # app_password 不传
        u = build_user_from_form(form, user_id="kong0001", existing=old)
        assert u.app_password_hash == old.app_password_hash
        assert verify_app_password(u, "old_pw_xyz") is True

    def test_new_password_overrides_existing(self):
        old = _existing_with_password()
        form = _form(name="kong", app_login_enabled="true", app_password="new_pw_456")
        u = build_user_from_form(form, user_id="kong0001", existing=old)
        assert u.app_password_hash != old.app_password_hash
        assert verify_app_password(u, "new_pw_456") is True
        assert verify_app_password(u, "old_pw_xyz") is False

    def test_clear_password_wipes_hash(self):
        old = _existing_with_password()
        form = _form(name="kong",
                     app_login_enabled="true",
                     app_password_clear="true")
        u = build_user_from_form(form, user_id="kong0001", existing=old)
        assert u.app_password_hash == ""
        # 即使开 app_login_enabled，verify 也会 fail-closed
        assert verify_app_password(u, "old_pw_xyz") is False

    def test_clear_takes_precedence_over_new_password(self):
        """同时填新密码 + 勾 clear → 以 clear 为准（防误操作）。"""
        old = _existing_with_password()
        form = _form(name="kong", app_login_enabled="true",
                     app_password="should_be_ignored",
                     app_password_clear="true")
        u = build_user_from_form(form, user_id="kong0001", existing=old)
        assert u.app_password_hash == ""

    def test_login_disabled_default(self):
        """不勾选 enabled checkbox → app_login_enabled=False。"""
        form = _form(name="kong", app_password="anything")
        u = build_user_from_form(form)
        assert u.app_login_enabled is False

    def test_login_explicitly_disabled(self):
        old = _existing_with_password()
        # checkbox 没勾 → 不出现在 form 中（HTML 行为）→ enabled=False
        form = _form(name="kong")
        u = build_user_from_form(form, user_id="kong0001", existing=old)
        assert u.app_login_enabled is False
        # 密码 hash 仍保留（admin 只是临时关）
        assert u.app_password_hash == old.app_password_hash


class TestRevokeOnPasswordChange:
    """密码修改后 token 应被撤销——这是 app/routes/users.py:user_edit 的责任。"""

    def test_edit_user_with_new_password_revokes_tokens(
        self, test_app, tmp_path, monkeypatch, isolated_data_dir
    ):
        # 隔离 DB 到 tmp_path 避免污染
        monkeypatch.setattr("app.db.DB_PATH", tmp_path / "edit.db")
        from app import api_auth
        api_auth.invalidate_token_cache()
        from app.db import storage
        from users import save_users
        # 准备：写一个用户 + 签一枚 token
        u = _existing_with_password()
        save_users([u])
        st = storage()
        try:
            _, plaintext = st.create_app_token(
                role="user", user_id=u.id, device_name="dev")
        finally:
            st.close()
        # 准备 admin client
        c = test_app.test_client()
        with c.session_transaction() as s:
            s["authenticated"] = True
            s["role"] = "admin"
            s["csrf_token"] = "test_csrf"
        # POST 修改密码（带所有必需字段）
        r = c.post(f"/users/{u.id}", data={
            "csrf_token": "test_csrf",
            "name": "kong",
            "enabled": "true",
            "NOTIFICATIONS_ENABLED": "true",
            "app_login_enabled": "true",
            "app_password": "totally_new_password",
        }, follow_redirects=False)
        assert r.status_code == 302  # redirect
        # 旧 token 必须已撤销
        st = storage()
        try:
            rows = st.list_app_tokens(user_id=u.id, include_revoked=True)
            assert len(rows) == 1
            assert rows[0]["revoked"] == 1
        finally:
            st.close()
        api_auth.invalidate_token_cache()
