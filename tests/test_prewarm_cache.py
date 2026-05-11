"""
monitor.py 的 Phase B 预登录缓存测试。

之前的 inline 冒烟测试覆盖了 9 项关键路径（首轮、命中、空轮、TTL 失效、
email 变更、unknown_error、用户禁用、清理、50 轮长跑）。本测试文件把
所有 9 项移植到 pytest 形态，加入 fixture，可重放。

测试不走真实网络 —— 全部用 mock 替换 create_prewarmed_session / try_book /
scrape_all，run_once 真实执行其余逻辑（diff、缓存查询、提交 executor、
await result、缓存失效判断）。
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import monitor
from monitor import _clear_prewarm_cache, _prewarmed_cache, run_once
from booker import BookingResult, PrewarmedSession
from notifier import BaseNotifier
from users import UserConfig
from config import AutoBookConfig, Config, CityFilter, AvailabilityFilter
from models import Listing
from storage import Storage


# ─── Helpers ──────────────────────────────────────────────────────


def _make_fake_prewarmed(email: str, ttl: float = 3300):
    sess = MagicMock()
    sess.closed = False
    def close_impl():
        sess.closed = True
    sess.close = MagicMock(side_effect=close_impl)
    return PrewarmedSession(
        session=sess, token="tok",
        created_at=time.monotonic(),
        token_expiry=time.monotonic() + ttl,
        email=email,
    )


class _FakeNotifier(BaseNotifier):
    has_channels = True
    async def _send(self, t): return True
    async def close(self): pass


def _make_listing(idx: int):
    """每轮生成唯一 id，避免 storage.diff 因 id 重复不再产出事件。"""
    return Listing(
        id=f"L-{idx}", name=f"Test-{idx}",
        status="Available to book", price_raw="€700",
        available_from="2030-01-01", features=[],
        url=f"https://t/{idx}", city="E",
        sku=f"SKU-{idx}", contract_id=42, contract_start_date="2030-01-01",
    )


# ─── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def clean_cache():
    """每个测试前后清空 _prewarmed_cache，避免污染。"""
    _clear_prewarm_cache()
    yield
    _clear_prewarm_cache()


@pytest.fixture
def fake_storage(tmp_path):
    """临时 SQLite，每个测试一个。"""
    s = Storage(tmp_path / "test.db", timezone_str="UTC")
    yield s
    s.close()


@pytest.fixture
def user_ab():
    """auto_book 启用的用户。"""
    return UserConfig(
        name="A", id="aaaa", enabled=True, notifications_enabled=True,
        notification_channels=[],
        auto_book=AutoBookConfig(enabled=True, email="a@x.com", password="pwA"),
    )


@pytest.fixture
def cfg():
    return Config(
        check_interval=300,
        cities=[CityFilter(name="E", id=29)],
        availability_filters=[AvailabilityFilter(label="A", id=179)],
        db_path=Path("data/listings.db"), log_level="WARNING",
    )


# ─── 各场景 ────────────────────────────────────────────────────────


class TestPrewarmCacheLifecycle:
    """
    通过 monkeypatch 替换 create_prewarmed_session / try_book / scrape_all，
    驱动 run_once 多次执行，验证缓存行为。
    """

    def _run(self, cfg, storage, notifs, prewarm_log, scrape_fn, try_book_fn=None):
        if try_book_fn is None:
            try_book_fn = lambda l, *a, **k: BookingResult(
                l, True, "ok", pay_url="https://pay", phase="success"
            )

        def fake_prewarm(email, password):
            prewarm_log.append(email)
            return _make_fake_prewarmed(email)

        async def go():
            with patch("monitor.create_prewarmed_session", side_effect=fake_prewarm), \
                 patch("monitor.try_book", side_effect=try_book_fn), \
                 patch("monitor.scrape_all", side_effect=scrape_fn):
                await run_once(cfg, storage, notifs, dry_run=False)

        asyncio.run(go())

    def test_first_round_with_candidate(self, clean_cache, fake_storage, cfg, user_ab):
        """首轮：cache 空 → 触发刷新 → 命中候选 → booking 成功 → 缓存留下。"""
        prewarm_log = []
        notifs = [(user_ab, _FakeNotifier())]
        scrape = lambda *a, **k: [_make_listing(1)]

        self._run(cfg, fake_storage, notifs, prewarm_log, scrape)

        assert len(prewarm_log) == 1, "首轮应触发 1 次登录"
        assert "aaaa" in _prewarmed_cache, "成功 booking 后应保留缓存"
        assert _prewarmed_cache["aaaa"].email == "a@x.com"

    def test_second_round_cache_hit_no_new_login(
        self, clean_cache, fake_storage, cfg, user_ab,
    ):
        """第二轮：缓存命中 → 0 新登录 → 同一个 session 实例继续使用。"""
        prewarm_log = []
        notifs = [(user_ab, _FakeNotifier())]
        scrape = lambda *a, **k: [_make_listing(1)]

        # 首轮：建立缓存
        self._run(cfg, fake_storage, notifs, prewarm_log, scrape)
        cached_session_id = id(_prewarmed_cache["aaaa"].session)

        # 第二轮（用不同 listing id 避免 diff 不产出事件）
        scrape2 = lambda *a, **k: [_make_listing(2)]
        self._run(cfg, fake_storage, notifs, prewarm_log, scrape2)

        assert len(prewarm_log) == 1, "缓存命中应该不再登录"
        assert id(_prewarmed_cache["aaaa"].session) == cached_session_id, \
            "缓存应保留同一个 session 实例"

    def test_empty_round_cache_survives(
        self, clean_cache, fake_storage, cfg, user_ab,
    ):
        """空候选轮（scrape 返回空）→ 缓存不被清空。这是 Phase B 核心收益。"""
        prewarm_log = []
        notifs = [(user_ab, _FakeNotifier())]

        # 首轮：建立缓存
        self._run(cfg, fake_storage, notifs, prewarm_log,
                  scrape_fn=lambda *a, **k: [_make_listing(1)])

        # 多轮空抓取
        for _ in range(5):
            self._run(cfg, fake_storage, notifs, prewarm_log,
                      scrape_fn=lambda *a, **k: [])

        assert len(prewarm_log) == 1, \
            "Phase B：空轮不应消耗 login。Phase A 行为会是 6"
        assert "aaaa" in _prewarmed_cache, "空轮后缓存应保留"

    def test_low_ttl_triggers_refresh(
        self, clean_cache, fake_storage, cfg, user_ab,
    ):
        """剩余 TTL < _TOKEN_REFRESH_MARGIN 触发刷新 + 关闭旧 session。"""
        prewarm_log = []
        notifs = [(user_ab, _FakeNotifier())]

        # 首轮：建立缓存
        self._run(cfg, fake_storage, notifs, prewarm_log,
                  scrape_fn=lambda *a, **k: [_make_listing(1)])
        assert len(prewarm_log) == 1

        # 手动篡改 TTL 让它 < 300s margin
        old = _prewarmed_cache["aaaa"]
        old.token_expiry = time.monotonic() + 60  # 余量 60s

        self._run(cfg, fake_storage, notifs, prewarm_log,
                  scrape_fn=lambda *a, **k: [])

        assert len(prewarm_log) == 2, "TTL 不足应触发刷新"
        assert old.session.closed is True, "旧 session 应被关闭"
        # 新 session 入缓存
        assert _prewarmed_cache["aaaa"].session is not old.session

    def test_email_change_invalidates_cache(
        self, clean_cache, fake_storage, cfg, user_ab,
    ):
        """用户改邮箱 → email 不匹配 → 缓存失效 + 重新登录。"""
        prewarm_log = []
        notifs = [(user_ab, _FakeNotifier())]

        self._run(cfg, fake_storage, notifs, prewarm_log,
                  scrape_fn=lambda *a, **k: [_make_listing(1)])
        old = _prewarmed_cache["aaaa"]

        # 修改 email
        user_ab.auto_book.email = "NEW@x.com"

        self._run(cfg, fake_storage, notifs, prewarm_log,
                  scrape_fn=lambda *a, **k: [_make_listing(2)])

        assert len(prewarm_log) == 2
        assert prewarm_log[1] == "NEW@x.com"
        assert _prewarmed_cache["aaaa"].email == "NEW@x.com"
        assert old.session.closed is True

    def test_unknown_error_invalidates_cache(
        self, clean_cache, fake_storage, cfg, user_ab,
    ):
        """booking 返回 unknown_error → 怀疑 session 损坏 → 失效。"""
        prewarm_log = []
        notifs = [(user_ab, _FakeNotifier())]

        # 建立缓存
        self._run(cfg, fake_storage, notifs, prewarm_log,
                  scrape_fn=lambda *a, **k: [_make_listing(1)])
        old = _prewarmed_cache["aaaa"]

        # 模拟 unknown_error
        unknown_fn = lambda l, *a, **k: BookingResult(
            l, False, "mystery", phase="unknown_error"
        )
        self._run(cfg, fake_storage, notifs, prewarm_log,
                  scrape_fn=lambda *a, **k: [_make_listing(2)],
                  try_book_fn=unknown_fn)

        assert "aaaa" not in _prewarmed_cache, \
            "unknown_error 应使缓存失效"
        assert old.session.closed is True

    def test_race_lost_keeps_cache(
        self, clean_cache, fake_storage, cfg, user_ab,
    ):
        """race_lost 是业务正常结果（被抢了），session 健康 → 缓存保留。"""
        prewarm_log = []
        notifs = [(user_ab, _FakeNotifier())]

        race_lost_fn = lambda l, *a, **k: BookingResult(
            l, False, "race_lost", phase="race_lost"
        )
        self._run(cfg, fake_storage, notifs, prewarm_log,
                  scrape_fn=lambda *a, **k: [_make_listing(1)],
                  try_book_fn=race_lost_fn)

        # race_lost 不应失效缓存
        assert "aaaa" in _prewarmed_cache, "race_lost session 健康，应保留"

    def test_user_disabled_evicts_cache(
        self, clean_cache, fake_storage, cfg, user_ab,
    ):
        """用户被禁用 / auto_book.enabled=False → 下一轮自动清理。"""
        prewarm_log = []
        notifs = [(user_ab, _FakeNotifier())]

        self._run(cfg, fake_storage, notifs, prewarm_log,
                  scrape_fn=lambda *a, **k: [_make_listing(1)])
        old = _prewarmed_cache["aaaa"]

        # 禁用 auto_book
        user_ab.auto_book.enabled = False

        self._run(cfg, fake_storage, notifs, prewarm_log,
                  scrape_fn=lambda *a, **k: [])

        assert "aaaa" not in _prewarmed_cache, \
            "auto_book 禁用后缓存应被淘汰"
        assert old.session.closed is True

    def test_clear_prewarm_cache_closes_all(self, clean_cache):
        """显式清理 → 全部 session.close()。"""
        sess1 = _make_fake_prewarmed("u1@x.com")
        sess2 = _make_fake_prewarmed("u2@x.com")
        _prewarmed_cache["u1"] = sess1
        _prewarmed_cache["u2"] = sess2

        _clear_prewarm_cache()

        assert _prewarmed_cache == {}
        assert sess1.session.closed
        assert sess2.session.closed


class TestPhaseBLongRunEconomy:
    """
    长跑场景：50 空轮 + 1 候选轮 → 1 次登录（vs Phase A 的 51 次）。
    """

    def test_50_empty_rounds_plus_one_booking(
        self, clean_cache, fake_storage, cfg, user_ab,
    ):
        prewarm_log = []
        notifs = [(user_ab, _FakeNotifier())]

        def run(scrape_fn):
            def fake_prewarm(e, p):
                prewarm_log.append(e)
                return _make_fake_prewarmed(e)

            async def go():
                with patch("monitor.create_prewarmed_session", side_effect=fake_prewarm), \
                     patch("monitor.try_book", side_effect=lambda l, *a, **k:
                           BookingResult(l, True, "ok", pay_url="x", phase="success")), \
                     patch("monitor.scrape_all", side_effect=scrape_fn):
                    await run_once(cfg, fake_storage, notifs, dry_run=False)
            asyncio.run(go())

        # 50 空轮
        for _ in range(50):
            run(lambda *a, **k: [])
        # 最后一轮命中
        run(lambda *a, **k: [_make_listing(99)])

        assert len(prewarm_log) == 1, (
            f"50 空轮 + 1 booking 应该只产生 1 次登录，"
            f"实际 {len(prewarm_log)} 次。Phase A 会是 51 次"
        )


class TestIsCachedSessionValid:
    """_is_cached_session_valid 的纯函数测试（无 IO）。"""

    def test_none_invalid(self):
        from monitor import _is_cached_session_valid
        assert _is_cached_session_valid(None, "a@x.com") is False

    def test_email_mismatch_invalid(self):
        from monitor import _is_cached_session_valid
        ps = _make_fake_prewarmed("a@x.com")
        assert _is_cached_session_valid(ps, "b@x.com") is False

    def test_ttl_below_margin_invalid(self):
        from monitor import _is_cached_session_valid, _TOKEN_REFRESH_MARGIN
        ps = _make_fake_prewarmed("a@x.com")
        # 余量 < margin
        ps.token_expiry = time.monotonic() + (_TOKEN_REFRESH_MARGIN - 10)
        assert _is_cached_session_valid(ps, "a@x.com") is False

    def test_ttl_above_margin_valid(self):
        from monitor import _is_cached_session_valid, _TOKEN_REFRESH_MARGIN
        ps = _make_fake_prewarmed("a@x.com")
        ps.token_expiry = time.monotonic() + (_TOKEN_REFRESH_MARGIN + 100)
        assert _is_cached_session_valid(ps, "a@x.com") is True

    def test_margin_is_substantial(self):
        """margin 必须远大于一次 booking 耗时（防止 try_book 内部触发过期路径
        从而 close 缓存中的 session）。"""
        from monitor import _TOKEN_REFRESH_MARGIN
        assert _TOKEN_REFRESH_MARGIN >= 60, "margin 太小：booking 可能耗尽 TTL"
