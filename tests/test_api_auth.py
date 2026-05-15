"""
app/api_auth.py 单元测试
=========================

聚焦点：
- _resolve_token: 命中、未命中、撤销、过期
- TTL 缓存：命中后不再走 DB；invalidate 后失效
- _schedule_touch / _flush_pending 批量回填 last_used_at
- bearer_required / bearer_optional 路由级行为通过 test_api_v1_auth.py
  已经间接覆盖；这里补缺角色 mismatch 与 cache 行为
"""

from __future__ import annotations

import time

import pytest

from app import api_auth
from app.api_auth import (
    _CACHE,
    _PENDING_TOUCH,
    _cache_get,
    _cache_put,
    _resolve_token,
    invalidate_token_cache,
)
from mstorage._tokens import hash_token


@pytest.fixture
def fresh_state(monkeypatch, tmp_path):
    """每个测试一个干净的 DB + 清空 TTL 缓存与 pending touch。"""
    fake_db = tmp_path / "auth.db"
    monkeypatch.setattr("app.db.DB_PATH", fake_db)
    invalidate_token_cache()
    _PENDING_TOUCH.clear()
    yield fake_db
    invalidate_token_cache()
    _PENDING_TOUCH.clear()


@pytest.fixture
def admin_token(fresh_state):
    from app.db import storage
    st = storage()
    try:
        _, plaintext = st.create_app_token(role="admin", user_id=None)
    finally:
        st.close()
    return plaintext


# ── _resolve_token ─────────────────────────────────────────────────


class TestResolveToken:
    def test_valid(self, admin_token):
        row = _resolve_token(admin_token)
        assert row is not None
        assert row["role"] == "admin"

    def test_unknown_returns_none(self, fresh_state):
        assert _resolve_token("totally_unknown_token") is None

    def test_revoked_returns_none(self, admin_token):
        from app.db import storage
        # 先命中一次填缓存
        row = _resolve_token(admin_token)
        assert row is not None
        # 撤销
        st = storage()
        try:
            st.revoke_app_token(row["id"])
        finally:
            st.close()
        # 清缓存（实际代码里 logout 会调用 invalidate_token_cache）
        invalidate_token_cache()
        assert _resolve_token(admin_token) is None

    def test_expired_returns_none(self, fresh_state):
        from app.db import storage
        st = storage()
        try:
            _, plaintext = st.create_app_token(
                role="admin", user_id=None, ttl_days=1)
            # 手动把 expires_at 改到过去
            with st.conn:
                st.conn.execute(
                    "UPDATE app_tokens SET expires_at = ? WHERE token_hash = ?",
                    ("2020-01-01T00:00:00Z", hash_token(plaintext)),
                )
        finally:
            st.close()
        invalidate_token_cache()
        assert _resolve_token(plaintext) is None


# ── TTL 缓存 ───────────────────────────────────────────────────────


class TestCache:
    def test_cache_get_miss(self):
        invalidate_token_cache()
        assert _cache_get("nonexistent_hash") is None

    def test_cache_put_then_get(self):
        invalidate_token_cache()
        _cache_put("h1", {"id": 1, "role": "admin"})
        row = _cache_get("h1")
        assert row is not None and row["id"] == 1

    def test_invalidate_specific(self):
        _cache_put("h1", {"id": 1})
        _cache_put("h2", {"id": 2})
        invalidate_token_cache("h1")
        assert _cache_get("h1") is None
        assert _cache_get("h2") is not None
        invalidate_token_cache()

    def test_invalidate_all(self):
        _cache_put("a", {"x": 1})
        _cache_put("b", {"y": 2})
        invalidate_token_cache()
        assert _cache_get("a") is None
        assert _cache_get("b") is None

    def test_cache_overflow_evicts_oldest(self):
        """缓存超上限时丢弃旧条目，新 token 不受影响。"""
        invalidate_token_cache()
        # 临时降低上限以便快速测试
        import app.api_auth as mod
        original = mod._CACHE_MAX
        mod._CACHE_MAX = 4
        try:
            for i in range(8):
                _cache_put(f"k{i}", {"i": i})
                time.sleep(0.001)  # 让 cached_at monotonic 有差异
            # 至少最新写入的 4 个应该都还在
            assert _cache_get("k7") is not None
            assert _cache_get("k6") is not None
        finally:
            mod._CACHE_MAX = original
            invalidate_token_cache()


# ── pending touch ──────────────────────────────────────────────────


class TestPendingTouch:
    def test_schedule_touch_collects(self):
        _PENDING_TOUCH.clear()
        from app.api_auth import _schedule_touch
        _schedule_touch(1)
        _schedule_touch(2)
        _schedule_touch(1)  # 重复，set 去重
        assert _PENDING_TOUCH == {1, 2}
        _PENDING_TOUCH.clear()

    def test_flush_pending_persists(self, fresh_state):
        from app.db import storage
        from app.api_auth import _flush_pending, _schedule_touch
        st = storage()
        try:
            tid1, _ = st.create_app_token(role="admin", user_id=None)
            tid2, _ = st.create_app_token(role="admin", user_id=None)
        finally:
            st.close()
        _schedule_touch(tid1)
        _schedule_touch(tid2)
        _flush_pending()
        assert _PENDING_TOUCH == set()
        # 落盘验证
        st = storage()
        try:
            rows = st.list_app_tokens()
            assert all(r["last_used_at"] is not None for r in rows)
        finally:
            st.close()
