"""
users.set_app_password / verify_app_password 单元测试。

覆盖：
- bcrypt 哈希生成与验证
- 空密码：set 清空 hash；verify 失败
- app_login_enabled=False 时 verify 必失败
- 异常输入 fail-closed
- 序列化往返：save → load 后 hash 仍可校验
"""

from __future__ import annotations

import pytest

from users import UserConfig, set_app_password, verify_app_password


def _make_user() -> UserConfig:
    u = UserConfig(name="kong", id="abc12345")
    u.app_login_enabled = True
    return u


class TestSetAppPassword:
    def test_sets_bcrypt_hash(self):
        u = _make_user()
        set_app_password(u, "hello")
        assert u.app_password_hash.startswith("$2")  # bcrypt 前缀
        assert "hello" not in u.app_password_hash

    def test_empty_clears(self):
        u = _make_user()
        set_app_password(u, "hello")
        assert u.app_password_hash
        set_app_password(u, "")
        assert u.app_password_hash == ""

    def test_different_each_call(self):
        """bcrypt salt 应保证同一密码两次 hash 不同。"""
        u1 = _make_user(); u2 = _make_user()
        set_app_password(u1, "same")
        set_app_password(u2, "same")
        assert u1.app_password_hash != u2.app_password_hash


class TestVerifyAppPassword:
    def test_correct_passes(self):
        u = _make_user()
        set_app_password(u, "secret_xyz")
        assert verify_app_password(u, "secret_xyz") is True

    def test_wrong_fails(self):
        u = _make_user()
        set_app_password(u, "secret_xyz")
        assert verify_app_password(u, "wrong") is False

    def test_empty_password_fails(self):
        u = _make_user()
        set_app_password(u, "secret")
        assert verify_app_password(u, "") is False

    def test_login_disabled_fails(self):
        u = _make_user()
        set_app_password(u, "secret")
        u.app_login_enabled = False
        assert verify_app_password(u, "secret") is False

    def test_no_hash_fails(self):
        u = _make_user()
        # 没设密码
        assert verify_app_password(u, "anything") is False

    def test_corrupted_hash_returns_false(self):
        u = _make_user()
        u.app_password_hash = "not-a-bcrypt-hash-at-all"
        assert verify_app_password(u, "anything") is False


class TestSerializationRoundTrip:
    def test_save_then_load_preserves_hash(self, isolated_data_dir):
        from users import load_users, save_users
        u = _make_user()
        set_app_password(u, "round-trip")
        save_users([u])

        loaded = load_users()
        assert len(loaded) == 1
        u2 = loaded[0]
        # hash 字段持久化
        assert u2.app_password_hash == u.app_password_hash
        assert u2.app_login_enabled is True
        # 校验仍然通过
        assert verify_app_password(u2, "round-trip") is True

    def test_old_users_json_without_fields_defaults_to_disabled(self, isolated_data_dir, tmp_path):
        """老 users.json 没有 app_login_enabled / app_password_hash → 默认禁用。"""
        import json
        from users import USERS_FILE, load_users
        # 写一份不带新字段的老数据
        legacy = [{
            "name": "legacy", "id": "legacy01", "enabled": True,
            "notifications_enabled": True, "notification_channels": [],
            "listing_filter": {}, "auto_book": {},
        }]
        USERS_FILE.write_text(json.dumps(legacy), encoding="utf-8")
        users = load_users()
        assert len(users) == 1
        assert users[0].app_login_enabled is False
        assert users[0].app_password_hash == ""
        assert verify_app_password(users[0], "anything") is False
