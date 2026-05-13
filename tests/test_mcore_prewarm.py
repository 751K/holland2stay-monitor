"""mcore/prewarm.py 单元测试 — PrewarmCache 类。"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from mcore.prewarm import PrewarmCache, _TOKEN_REFRESH_MARGIN
from booker import PrewarmedSession


def _make_fake_prewarmed(email: str, ttl: float = 3300):
    sess = MagicMock()
    sess.closed = False

    def close_impl():
        sess.closed = True

    sess.close = MagicMock(side_effect=close_impl)
    return PrewarmedSession(
        session=sess,
        token="tok",
        created_at=time.monotonic(),
        token_expiry=time.monotonic() + ttl,
        email=email,
    )


# ── 基本 CRUD ─────────────────────────────────────────────────────


class TestPrewarmCacheBasics:
    def test_new_cache_is_empty(self):
        pc = PrewarmCache()
        assert len(pc) == 0
        assert pc.get("any") is None

    def test_set_and_get(self):
        pc = PrewarmCache()
        ps = _make_fake_prewarmed("a@x.com")
        pc.set("u1", ps)
        assert len(pc) == 1
        assert pc.get("u1") is ps
        assert pc.get("u1").email == "a@x.com"

    def test_contains(self):
        pc = PrewarmCache()
        assert "u1" not in pc
        pc.set("u1", _make_fake_prewarmed("a@x.com"))
        assert "u1" in pc

    def test_keys(self):
        pc = PrewarmCache()
        pc.set("u1", _make_fake_prewarmed("a@x.com"))
        pc.set("u2", _make_fake_prewarmed("b@x.com"))
        assert set(pc.keys()) == {"u1", "u2"}

    def test_overwrite_updates(self):
        pc = PrewarmCache()
        ps1 = _make_fake_prewarmed("a@x.com")
        ps2 = _make_fake_prewarmed("a@x.com")
        pc.set("u1", ps1)
        pc.set("u1", ps2)
        assert pc.get("u1") is ps2


# ── is_valid ──────────────────────────────────────────────────────


class TestPrewarmCacheIsValid:
    def test_none_invalid(self):
        pc = PrewarmCache()
        assert pc.is_valid(None, "a@x.com") is False

    def test_email_mismatch(self):
        pc = PrewarmCache()
        ps = _make_fake_prewarmed("a@x.com")
        assert pc.is_valid(ps, "b@x.com") is False

    def test_ttl_below_margin(self):
        pc = PrewarmCache()
        ps = _make_fake_prewarmed("a@x.com")
        ps.token_expiry = time.monotonic() + (_TOKEN_REFRESH_MARGIN - 10)
        assert pc.is_valid(ps, "a@x.com") is False

    def test_ttl_above_margin(self):
        pc = PrewarmCache()
        ps = _make_fake_prewarmed("a@x.com")
        ps.token_expiry = time.monotonic() + (_TOKEN_REFRESH_MARGIN + 100)
        assert pc.is_valid(ps, "a@x.com") is True

    def test_margin_is_substantial(self):
        assert _TOKEN_REFRESH_MARGIN >= 60, "margin 太小"


# ── invalidate ────────────────────────────────────────────────────


class TestPrewarmCacheInvalidate:
    def test_invalidate_removes_and_closes(self):
        pc = PrewarmCache()
        ps = _make_fake_prewarmed("a@x.com")
        pc.set("u1", ps)
        pc.invalidate("u1")
        assert "u1" not in pc
        assert ps.session.closed is True

    def test_invalidate_unknown_is_noop(self):
        pc = PrewarmCache()
        pc.invalidate("noone")  # no error

    def test_invalidate_none_session(self):
        """如果 session 为 None，pop 返回 None，不调 close。"""
        pc = PrewarmCache()
        pc._cache["u1"] = None  # corner case
        pc.invalidate("u1")  # no AttributeError


# ── clear ─────────────────────────────────────────────────────────


class TestPrewarmCacheClear:
    def test_clear_closes_all(self):
        pc = PrewarmCache()
        ps1 = _make_fake_prewarmed("a@x.com")
        ps2 = _make_fake_prewarmed("b@x.com")
        pc.set("u1", ps1)
        pc.set("u2", ps2)
        pc.clear()
        assert len(pc) == 0
        assert ps1.session.closed is True
        assert ps2.session.closed is True

    def test_clear_empty_is_noop(self):
        pc = PrewarmCache()
        pc.clear()  # no error


# ── create ────────────────────────────────────────────────────────


class TestPrewarmCacheCreate:
    def test_create_success(self):
        with patch("mcore.prewarm.create_prewarmed_session") as mock_create:
            ps = _make_fake_prewarmed("a@x.com")
            mock_create.return_value = ps

            user = MagicMock()
            user.name = "A"
            user.auto_book.email = "a@x.com"
            user.auto_book.password = "pw"

            pc = PrewarmCache()
            result = pc.create(user)
            assert result is ps
            mock_create.assert_called_once_with("a@x.com", "pw")

    def test_create_failure_returns_none(self):
        with patch("mcore.prewarm.create_prewarmed_session") as mock_create:
            mock_create.side_effect = Exception("network error")

            user = MagicMock()
            user.name = "A"
            user.auto_book.email = "a@x.com"
            user.auto_book.password = "pw"

            pc = PrewarmCache()
            result = pc.create(user)
            assert result is None
