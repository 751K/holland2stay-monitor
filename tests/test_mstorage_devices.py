"""
mstorage._devices DeviceOps 单元测试
======================================

覆盖：
- register_device 新建 / 同 (token,session) 刷新 / disable 复活
- 输入校验：bad env / 过短 device_token
- list_devices_for_token: 按会话隔离
- get_active_devices_for_user: JOIN app_tokens 过滤 revoked / disabled
- disable_device / disable_device_by_token / delete_device
"""

from __future__ import annotations

import pytest


# ── register ───────────────────────────────────────────────────────


class TestRegister:
    def _seed_token(self, db, role="user", user_id="kong0001"):
        tid, _ = db.create_app_token(role=role, user_id=user_id)
        return tid

    def test_register_new(self, temp_db):
        tid = self._seed_token(temp_db)
        did = temp_db.register_device(
            app_token_id=tid,
            device_token="a" * 64,
            env="production",
            model="iPhone15,2",
            bundle_id="com.x.y",
        )
        assert did > 0
        rows = temp_db.list_devices_for_token(tid)
        assert len(rows) == 1
        assert rows[0]["device_token"] == "a" * 64

    def test_register_same_refreshes(self, temp_db):
        """重复注册同 (app_token, device_token) → 刷新而非新增。"""
        tid = self._seed_token(temp_db)
        d1 = temp_db.register_device(
            app_token_id=tid, device_token="b" * 64, env="production",
        )
        d2 = temp_db.register_device(
            app_token_id=tid, device_token="b" * 64, env="sandbox", model="X",
        )
        assert d1 == d2
        rows = temp_db.list_devices_for_token(tid)
        assert len(rows) == 1
        assert rows[0]["env"] == "sandbox"
        assert rows[0]["model"] == "X"

    def test_re_register_revives_disabled(self, temp_db):
        tid = self._seed_token(temp_db)
        did = temp_db.register_device(
            app_token_id=tid, device_token="c" * 64,
        )
        temp_db.disable_device(did, reason="Unregistered")
        # disabled
        assert temp_db.get_device(did)["disabled_at"] is not None
        # 用户重装 App → 再注册同 token → 复活
        did2 = temp_db.register_device(
            app_token_id=tid, device_token="c" * 64,
        )
        assert did2 == did
        assert temp_db.get_device(did)["disabled_at"] is None

    def test_bad_env_rejected(self, temp_db):
        tid = self._seed_token(temp_db)
        with pytest.raises(ValueError):
            temp_db.register_device(
                app_token_id=tid, device_token="d" * 64, env="weird",
            )

    def test_short_token_rejected(self, temp_db):
        tid = self._seed_token(temp_db)
        with pytest.raises(ValueError):
            temp_db.register_device(
                app_token_id=tid, device_token="short", env="production",
            )


# ── 查询 ────────────────────────────────────────────────────────────


class TestQueries:
    def test_active_devices_per_user(self, temp_db):
        # 两个用户各一台设备
        tA, _ = temp_db.create_app_token(role="user", user_id="userA")
        tB, _ = temp_db.create_app_token(role="user", user_id="userB")
        temp_db.register_device(app_token_id=tA, device_token="a" * 64)
        temp_db.register_device(app_token_id=tB, device_token="b" * 64)
        assert len(temp_db.get_active_devices_for_user("userA")) == 1
        assert len(temp_db.get_active_devices_for_user("userB")) == 1
        assert len(temp_db.get_active_devices_for_user("ghost")) == 0

    def test_revoked_token_excludes_device(self, temp_db):
        tid, _ = temp_db.create_app_token(role="user", user_id="userX")
        temp_db.register_device(app_token_id=tid, device_token="x" * 64)
        temp_db.revoke_app_token(tid)
        # revoked → 不再可推
        assert temp_db.get_active_devices_for_user("userX") == []

    def test_disabled_device_excludes(self, temp_db):
        tid, _ = temp_db.create_app_token(role="user", user_id="userY")
        did = temp_db.register_device(
            app_token_id=tid, device_token="y" * 64,
        )
        temp_db.disable_device(did, reason="Unregistered")
        assert temp_db.get_active_devices_for_user("userY") == []


# ── 失活 / 删除 ────────────────────────────────────────────────────


class TestDisableDelete:
    def test_disable_idempotent(self, temp_db):
        tid, _ = temp_db.create_app_token(role="user", user_id="u")
        did = temp_db.register_device(app_token_id=tid, device_token="z" * 64)
        assert temp_db.disable_device(did, reason="x") is True
        # 二次 disable 已是 disabled → no-op
        assert temp_db.disable_device(did, reason="x") is False

    def test_disable_by_token_bulk(self, temp_db):
        """同一 device_token 可能跨多个 app_token；by_token 批量失效。"""
        tA, _ = temp_db.create_app_token(role="user", user_id="A")
        tB, _ = temp_db.create_app_token(role="user", user_id="B")
        temp_db.register_device(app_token_id=tA, device_token="dup" + "0" * 61)
        temp_db.register_device(app_token_id=tB, device_token="dup" + "0" * 61)
        n = temp_db.disable_device_by_token("dup" + "0" * 61, reason="Unregistered")
        assert n == 2

    def test_delete_device(self, temp_db):
        tid, _ = temp_db.create_app_token(role="user", user_id="u")
        did = temp_db.register_device(app_token_id=tid, device_token="e" * 64)
        assert temp_db.delete_device(did) is True
        assert temp_db.get_device(did) is None
        assert temp_db.delete_device(did) is False
