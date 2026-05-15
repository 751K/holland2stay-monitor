"""
mstorage._tokens TokenOps 单元测试。

覆盖：
- generate_token / hash_token 工具
- create_app_token: admin/user 角色约束、明文 token 返回与库内 hash
- find_app_token: 命中、未命中、已撤销、已过期
- list_app_tokens: 过滤 user_id / include_revoked
- revoke_app_token / revoke_user_tokens
- touch_app_tokens 批量更新 last_used_at
- cleanup_expired_tokens 物理回收
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from mstorage._tokens import generate_token, hash_token


# ── 工具函数 ────────────────────────────────────────────────────────


class TestTokenHelpers:
    def test_generate_unique(self):
        toks = {generate_token() for _ in range(50)}
        assert len(toks) == 50  # 极小概率碰撞，足以视为唯一

    def test_generate_length_reasonable(self):
        tok = generate_token()
        assert 30 <= len(tok) <= 64

    def test_hash_deterministic(self):
        assert hash_token("abc") == hash_token("abc")

    def test_hash_different_for_different_input(self):
        assert hash_token("a") != hash_token("b")

    def test_hash_is_hex_sha256(self):
        h = hash_token("anything")
        assert len(h) == 64
        int(h, 16)  # 不抛 = 合法十六进制


# ── 签发 ────────────────────────────────────────────────────────────


class TestCreateAppToken:
    def test_create_admin_token(self, temp_db):
        tid, plaintext = temp_db.create_app_token(
            role="admin", user_id=None, device_name="iPhone X")
        assert tid > 0
        assert isinstance(plaintext, str) and plaintext

        row = temp_db.find_app_token(hash_token(plaintext))
        assert row is not None
        assert row["role"] == "admin"
        assert row["user_id"] is None
        assert row["device_name"] == "iPhone X"
        assert row["revoked"] == 0
        assert row["token_hash"] != plaintext  # 库里存的是 hash

    def test_create_user_token_requires_user_id(self, temp_db):
        with pytest.raises(ValueError):
            temp_db.create_app_token(role="user", user_id=None)

    def test_admin_must_not_carry_user_id(self, temp_db):
        with pytest.raises(ValueError):
            temp_db.create_app_token(role="admin", user_id="abc")

    def test_invalid_role(self, temp_db):
        with pytest.raises(ValueError):
            temp_db.create_app_token(role="root", user_id="x")  # type: ignore[arg-type]

    def test_no_ttl_means_no_expires(self, temp_db):
        _, t = temp_db.create_app_token(role="admin", user_id=None, ttl_days=None)
        row = temp_db.find_app_token(hash_token(t))
        assert row["expires_at"] is None

    def test_ttl_sets_expires_at(self, temp_db):
        _, t = temp_db.create_app_token(role="admin", user_id=None, ttl_days=30)
        row = temp_db.find_app_token(hash_token(t))
        assert row["expires_at"] is not None
        # 约 30 天后；允许一点漂移
        future = datetime.now(timezone.utc) + timedelta(days=29)
        assert row["expires_at"] > future.strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_plaintext_is_unique_across_calls(self, temp_db):
        _, t1 = temp_db.create_app_token(role="admin", user_id=None)
        _, t2 = temp_db.create_app_token(role="admin", user_id=None)
        assert t1 != t2


# ── 查询 ────────────────────────────────────────────────────────────


class TestFindAppToken:
    def test_unknown_returns_none(self, temp_db):
        assert temp_db.find_app_token("0" * 64) is None

    def test_revoked_still_returned(self, temp_db):
        """find 不自动过滤——由 api_auth 层判断 revoked/expired。"""
        tid, t = temp_db.create_app_token(role="admin", user_id=None)
        temp_db.revoke_app_token(tid)
        row = temp_db.find_app_token(hash_token(t))
        assert row is not None
        assert row["revoked"] == 1


# ── 列表 ────────────────────────────────────────────────────────────


class TestListAppTokens:
    def test_filter_by_user(self, temp_db):
        temp_db.create_app_token(role="admin", user_id=None)
        temp_db.create_app_token(role="user", user_id="aaa11111")
        temp_db.create_app_token(role="user", user_id="bbb22222")

        only_a = temp_db.list_app_tokens(user_id="aaa11111")
        assert len(only_a) == 1
        assert only_a[0]["user_id"] == "aaa11111"

    def test_excludes_revoked_by_default(self, temp_db):
        tid, _ = temp_db.create_app_token(role="admin", user_id=None)
        temp_db.revoke_app_token(tid)
        assert temp_db.list_app_tokens() == []
        assert len(temp_db.list_app_tokens(include_revoked=True)) == 1

    def test_orders_newest_first(self, temp_db):
        temp_db.create_app_token(role="admin", user_id=None, device_name="A")
        time.sleep(1.01)  # 跨秒，created_at 字符串可区分
        temp_db.create_app_token(role="admin", user_id=None, device_name="B")
        rows = temp_db.list_app_tokens()
        assert rows[0]["device_name"] == "B"
        assert rows[1]["device_name"] == "A"


# ── 撤销 ────────────────────────────────────────────────────────────


class TestRevoke:
    def test_revoke_single(self, temp_db):
        tid, _ = temp_db.create_app_token(role="admin", user_id=None)
        assert temp_db.revoke_app_token(tid) is True
        # 二次撤销 no-op
        assert temp_db.revoke_app_token(tid) is False

    def test_revoke_unknown_id(self, temp_db):
        assert temp_db.revoke_app_token(999999) is False

    def test_revoke_user_tokens_bulk(self, temp_db):
        temp_db.create_app_token(role="user", user_id="userX")
        temp_db.create_app_token(role="user", user_id="userX")
        temp_db.create_app_token(role="user", user_id="userY")
        n = temp_db.revoke_user_tokens("userX")
        assert n == 2
        # 再次撤销已撤销的应该返回 0
        assert temp_db.revoke_user_tokens("userX") == 0
        # userY 不受影响
        assert len(temp_db.list_app_tokens(user_id="userY")) == 1


# ── last_used_at ────────────────────────────────────────────────────


class TestTouch:
    def test_empty_is_noop(self, temp_db):
        temp_db.touch_app_tokens([])  # 不抛

    def test_batch_update(self, temp_db):
        ids = []
        for _ in range(3):
            tid, _ = temp_db.create_app_token(role="admin", user_id=None)
            ids.append(tid)
        for r in temp_db.list_app_tokens():
            assert r["last_used_at"] is None
        temp_db.touch_app_tokens(ids)
        for r in temp_db.list_app_tokens():
            assert r["last_used_at"] is not None


# ── cleanup ─────────────────────────────────────────────────────────


class TestCleanup:
    def test_keeps_active_tokens(self, temp_db):
        temp_db.create_app_token(role="admin", user_id=None)
        n = temp_db.cleanup_expired_tokens(keep_revoked_days=30)
        assert n == 0

    def test_removes_old_revoked(self, temp_db):
        tid, _ = temp_db.create_app_token(role="admin", user_id=None)
        temp_db.revoke_app_token(tid)
        # 把 created_at 手工设到很久以前
        with temp_db.conn:
            temp_db.conn.execute(
                "UPDATE app_tokens SET created_at = ? WHERE id = ?",
                ("2020-01-01T00:00:00Z", tid),
            )
        n = temp_db.cleanup_expired_tokens(keep_revoked_days=30)
        assert n == 1
        assert temp_db.find_app_token("nope" * 16) is None
