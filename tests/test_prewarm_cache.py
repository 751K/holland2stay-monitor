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
from monitor import run_once

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
        fetcher=sess, token="tok",
        created_at=time.monotonic(),
        token_expiry=time.monotonic() + ttl,
        email=email,
    )


class _FakeNotifier(BaseNotifier):
    has_channels = True
    async def _send(self, t): return True
    async def close(self): pass


def _make_listing(idx: int):
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
    """每个测试前后清空 prewarm_cache，避免污染。"""
    monitor.prewarm_cache.clear()
    yield
    monitor.prewarm_cache.clear()


@pytest.fixture
def fake_storage(tmp_path):
    s = Storage(tmp_path / "test.db", timezone_str="UTC")
    yield s
    s.close()


@pytest.fixture
def user_ab():
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


# ── 缓存别名（缩短测试行宽）────────────────────────────────────────

_pc = monitor.prewarm_cache  # PrewarmCache 实例


# ─── 各场景 ────────────────────────────────────────────────────────


class TestPrewarmCacheLifecycle:

    def _run(self, cfg, storage, notifs, prewarm_log, scrape_fn, try_book_fn=None):
        if try_book_fn is None:
            try_book_fn = lambda l, *a, **k: BookingResult(
                l, True, "ok", pay_url="https://pay", phase="success"
            )

        def fake_prewarm(email, password, **kw):
            prewarm_log.append(email)
            return _make_fake_prewarmed(email)

        async def go():
            with patch("mcore.prewarm.create_prewarmed_session", side_effect=fake_prewarm), \
                 patch("bookers.holland2stay.try_book", side_effect=try_book_fn), \
                 patch("monitor.dispatch_scrape_tasks", side_effect=scrape_fn):
                await run_once(cfg, storage, notifs, dry_run=False)

        asyncio.run(go())

    def test_first_round_with_candidate(self, clean_cache, fake_storage, cfg, user_ab):
        prewarm_log = []
        notifs = [(user_ab, _FakeNotifier())]
        scrape = lambda *a, **k: [_make_listing(1)]

        self._run(cfg, fake_storage, notifs, prewarm_log, scrape)

        assert len(prewarm_log) == 1, "首轮应触发 1 次登录"
        assert "aaaa" in _pc, "成功 booking 后应保留缓存"
        assert _pc.get("aaaa").email == "a@x.com"

    def test_second_round_cache_hit_no_new_login(
        self, clean_cache, fake_storage, cfg, user_ab,
    ):
        prewarm_log = []
        notifs = [(user_ab, _FakeNotifier())]
        scrape = lambda *a, **k: [_make_listing(1)]

        self._run(cfg, fake_storage, notifs, prewarm_log, scrape)
        cached_session_id = id(_pc.get("aaaa").fetcher)

        scrape2 = lambda *a, **k: [_make_listing(2)]
        self._run(cfg, fake_storage, notifs, prewarm_log, scrape2)

        assert len(prewarm_log) == 1, "缓存命中应该不再登录"
        assert id(_pc.get("aaaa").fetcher) == cached_session_id, \
            "缓存应保留同一个 session 实例"

    def test_empty_round_cache_survives(
        self, clean_cache, fake_storage, cfg, user_ab,
    ):
        prewarm_log = []
        notifs = [(user_ab, _FakeNotifier())]

        self._run(cfg, fake_storage, notifs, prewarm_log,
                  scrape_fn=lambda *a, **k: [_make_listing(1)])

        for _ in range(5):
            self._run(cfg, fake_storage, notifs, prewarm_log,
                      scrape_fn=lambda *a, **k: [])

        assert len(prewarm_log) == 1, \
            "Phase B：空轮不应消耗 login。Phase A 行为会是 6"
        assert "aaaa" in _pc, "空轮后缓存应保留"

    def test_low_ttl_refresh_waits_for_next_candidate(
        self, clean_cache, fake_storage, cfg, user_ab,
    ):
        prewarm_log = []
        notifs = [(user_ab, _FakeNotifier())]

        self._run(cfg, fake_storage, notifs, prewarm_log,
                  scrape_fn=lambda *a, **k: [_make_listing(1)])
        assert len(prewarm_log) == 1

        old = _pc.get("aaaa")
        old.token_expiry = time.monotonic() + 60  # 余量 60s < margin

        self._run(cfg, fake_storage, notifs, prewarm_log,
                  scrape_fn=lambda *a, **k: [])

        assert len(prewarm_log) == 1, "无候选空轮不应为了刷新 TTL 触碰登录接口"
        assert _pc.get("aaaa") is old

        self._run(cfg, fake_storage, notifs, prewarm_log,
                  scrape_fn=lambda *a, **k: [_make_listing(2)])

        assert len(prewarm_log) == 2, "TTL 不足应触发刷新"
        assert old.fetcher.closed is True, "旧 session 应被关闭"
        assert _pc.get("aaaa").fetcher is not old.fetcher

    def test_email_change_invalidates_cache(
        self, clean_cache, fake_storage, cfg, user_ab,
    ):
        prewarm_log = []
        notifs = [(user_ab, _FakeNotifier())]

        self._run(cfg, fake_storage, notifs, prewarm_log,
                  scrape_fn=lambda *a, **k: [_make_listing(1)])
        old = _pc.get("aaaa")

        user_ab.auto_book.email = "NEW@x.com"

        self._run(cfg, fake_storage, notifs, prewarm_log,
                  scrape_fn=lambda *a, **k: [_make_listing(2)])

        assert len(prewarm_log) == 2
        assert prewarm_log[1] == "NEW@x.com"
        assert _pc.get("aaaa").email == "NEW@x.com"
        assert old.fetcher.closed is True

    def test_unknown_error_invalidates_cache(
        self, clean_cache, fake_storage, cfg, user_ab,
    ):
        prewarm_log = []
        notifs = [(user_ab, _FakeNotifier())]

        self._run(cfg, fake_storage, notifs, prewarm_log,
                  scrape_fn=lambda *a, **k: [_make_listing(1)])
        old = _pc.get("aaaa")

        unknown_fn = lambda l, *a, **k: BookingResult(
            l, False, "mystery", phase="unknown_error"
        )
        self._run(cfg, fake_storage, notifs, prewarm_log,
                  scrape_fn=lambda *a, **k: [_make_listing(2)],
                  try_book_fn=unknown_fn)

        assert "aaaa" not in _pc, "unknown_error 应使缓存失效"
        assert old.fetcher.closed is True

    def test_race_lost_keeps_cache(
        self, clean_cache, fake_storage, cfg, user_ab,
    ):
        prewarm_log = []
        notifs = [(user_ab, _FakeNotifier())]

        race_lost_fn = lambda l, *a, **k: BookingResult(
            l, False, "race_lost", phase="race_lost"
        )
        self._run(cfg, fake_storage, notifs, prewarm_log,
                  scrape_fn=lambda *a, **k: [_make_listing(1)],
                  try_book_fn=race_lost_fn)

        assert "aaaa" in _pc, "race_lost session 健康，应保留"

    def test_user_disabled_evicts_cache(
        self, clean_cache, fake_storage, cfg, user_ab,
    ):
        prewarm_log = []
        notifs = [(user_ab, _FakeNotifier())]

        self._run(cfg, fake_storage, notifs, prewarm_log,
                  scrape_fn=lambda *a, **k: [_make_listing(1)])
        old = _pc.get("aaaa")

        user_ab.auto_book.enabled = False

        self._run(cfg, fake_storage, notifs, prewarm_log,
                  scrape_fn=lambda *a, **k: [])

        assert "aaaa" not in _pc, "auto_book 禁用后缓存应被淘汰"
        assert old.fetcher.closed is True

    def test_clear_prewarm_cache_closes_all(self, clean_cache):
        sess1 = _make_fake_prewarmed("u1@x.com")
        sess2 = _make_fake_prewarmed("u2@x.com")
        _pc.set("u1", sess1)
        _pc.set("u2", sess2)

        _pc.clear()

        assert len(_pc) == 0
        assert sess1.fetcher.closed
        assert sess2.fetcher.closed


class TestPhaseBLongRunEconomy:

    def test_50_empty_rounds_plus_one_booking(
        self, clean_cache, fake_storage, cfg, user_ab,
    ):
        prewarm_log = []
        notifs = [(user_ab, _FakeNotifier())]

        def run(scrape_fn):
            def fake_prewarm(e, p, **kw):
                prewarm_log.append(e)
                return _make_fake_prewarmed(e)

            async def go():
                with patch("mcore.prewarm.create_prewarmed_session", side_effect=fake_prewarm), \
                     patch("bookers.holland2stay.try_book", side_effect=lambda l, *a, **k:
                           BookingResult(l, True, "ok", pay_url="x", phase="success")), \
                     patch("monitor.dispatch_scrape_tasks", side_effect=scrape_fn):
                    await run_once(cfg, fake_storage, notifs, dry_run=False)
            asyncio.run(go())

        for _ in range(50):
            run(lambda *a, **k: [])
        run(lambda *a, **k: [_make_listing(99)])

        assert len(prewarm_log) == 1, (
            f"50 空轮 + 1 booking 应该只产生 1 次登录，"
            f"实际 {len(prewarm_log)} 次。Phase A 会是 51 次"
        )

    def test_ourdomain_listing_does_not_trigger_h2s_booking(
        self, clean_cache, fake_storage, cfg, user_ab
    ):
        listing = _make_listing(1)
        listing.id = "od_307195"
        listing.source = "ourdomain"
        listing.sku = ""
        notifs = [(user_ab, _FakeNotifier())]

        async def go():
            with patch("monitor.dispatch_scrape_tasks", return_value=([listing], {"ourdomain:Amsterdam Diemen": True})), \
                 patch("mcore.prewarm.create_prewarmed_session", return_value=None), \
                 patch("bookers.holland2stay.try_book") as try_book:
                await run_once(cfg, fake_storage, notifs, dry_run=False)
                try_book.assert_not_called()

        asyncio.run(go())


class TestWhoGetsTheWarmLane:
    """常驻浏览器给谁——走真的 run_once，不看源码。

    这一族的问题是 2026-09-08 被问出来的：「排名 1 的用户没开自动预订、排名 2 的
    开了，怎么办？」当时代码是对的（资格过滤排在借用决策前面），但**一条测试都
    没有**——也就是说它当时是对的，明天被谁重排一下循环就不是了，而且不会有任何
    东西变红。

    这里断言的是 create() 收到的 ``may_borrow_lane``：谁拿到 True，常驻就是谁的。
    """

    def _run(self, cfg, storage, notifs, log, listings):
        def scrape(*a, **k):
            return listings, {}

        def fake_create(user, *, may_borrow_lane=True):
            log.append((user.name, may_borrow_lane))
            return _make_fake_prewarmed(user.auto_book.email)

        async def go():
            with patch.object(monitor.prewarm_cache, "create",
                              side_effect=fake_create), \
                 patch("bookers.holland2stay.try_book",
                       side_effect=lambda l, *a, **k: BookingResult(
                           l, True, "ok", pay_url="https://p", phase="success")), \
                 patch("monitor.dispatch_scrape_tasks", side_effect=scrape):
                await run_once(cfg, storage, notifs, dry_run=False)

        asyncio.run(go())

    @staticmethod
    def _user(name, uid, *, auto_book: bool):
        return UserConfig(
            name=name, id=uid, enabled=True, notifications_enabled=True,
            notification_channels=[],
            auto_book=AutoBookConfig(enabled=auto_book,
                                     email=f"{uid}@x.com", password="pw"),
        )

    def test_an_ineligible_first_user_does_not_consume_the_lane(
            self, clean_cache, fake_storage, cfg):
        """排名 1 没开自动预订 → 常驻归排名 2。

        资格过滤（active_user_ids / candidate_user_ids）必须排在借用决策**前面**。
        排反了的话，常驻会被一个压根不下单的人「占掉」，于是真正要用的人每次都
        走冷启动——而日志里什么都看不出来。
        """
        # 名字刻意与优先级**逆序**（Zeta 排第一、Alpha 排第二）。
        # 起成 First/Second/Third 的话，「按名字排序」这个变异改不动顺序，
        # 于是「循环被重排」这类缺陷测不出来——第一版就是这么写的。
        notifs = [
            (self._user("Zeta", "u1", auto_book=False), _FakeNotifier()),
            (self._user("Alpha", "u2", auto_book=True), _FakeNotifier()),
            (self._user("Mu", "u3", auto_book=True), _FakeNotifier()),
        ]
        log = []
        self._run(cfg, fake_storage, notifs, log, [_make_listing(1), _make_listing(2)])

        assert log, "一个 prewarm 都没触发，这条测试什么也没测到"
        assert dict(log).get("Zeta") is None, "没开自动预订的人被 prewarm 了"
        borrowers = [n for n, may in log if may]
        assert borrowers == ["Alpha"], f"常驻给错人了: {log}"

    def test_the_lane_goes_to_exactly_one_user(
            self, clean_cache, fake_storage, cfg):
        """两个人都拿 True 就等于回到排队——而那个队列不认 sort_order。"""
        # 同上：名字与优先级逆序，好让「循环被重排」露出来。
        notifs = [
            (self._user("Zeta", "u1", auto_book=True), _FakeNotifier()),
            (self._user("Alpha", "u2", auto_book=True), _FakeNotifier()),
        ]
        log = []
        self._run(cfg, fake_storage, notifs, log, [_make_listing(1), _make_listing(2)])

        assert sum(1 for _, may in log if may) == 1, f"借用者不止一个: {log}"
        assert log[0] == ("Zeta", True), f"没给优先级最高的那个: {log}"


class TestHeartbeatRunsOffTheEventLoop:
    """常驻浏览器的心跳必须在 executor 线程里跑。

    ``BrowserFetcher`` 用的是 Playwright 的**同步** API，而它拒绝在一个正在跑的
    asyncio loop 里工作：

        It looks like you are using Playwright Sync API inside the asyncio loop.

    2026-09-08 上线当轮就撞上了。之前的单测拿假 fetcher 直接调 heartbeat()，没有
    loop、也没有真的 Playwright，所以这个约束完全不可见——测得再密也照不到。

    这条从 run_once 里调，断言心跳看到的是「没有正在运行的 loop」。
    """

    def test_heartbeat_is_not_called_on_the_loop_thread(
            self, clean_cache, fake_storage, cfg, user_ab):
        seen = []

        def _fake_heartbeat(*, wanted, may_rebuild):
            try:
                asyncio.get_running_loop()
                seen.append("loop")     # 在事件循环线程上 —— Playwright 会拒绝
            except RuntimeError:
                seen.append("thread")   # 干净的 executor 线程

        def scrape(*a, **k):
            return [_make_listing(1)], {}

        async def go():
            from mcore import warm_browser
            with patch.object(warm_browser.warm_lane, "heartbeat",
                              side_effect=_fake_heartbeat), \
                 patch("mcore.prewarm.create_prewarmed_session",
                       side_effect=lambda e, p, **kw: _make_fake_prewarmed(e)), \
                 patch("bookers.holland2stay.try_book",
                       side_effect=lambda l, *a, **k: BookingResult(
                           l, True, "ok", pay_url="https://p", phase="success")), \
                 patch("monitor.dispatch_scrape_tasks", side_effect=scrape):
                await run_once(cfg, fake_storage, [(user_ab, _FakeNotifier())],
                               dry_run=False)
                # 心跳是 fire-and-forget，给 executor 一点时间跑完
                for _ in range(50):
                    if seen:
                        break
                    await asyncio.sleep(0.02)

        asyncio.run(go())

        assert seen, "心跳压根没被调用"
        assert seen[0] == "thread", (
            "心跳跑在事件循环线程上——Playwright 同步 API 会直接拒绝，"
            "常驻浏览器永远建不起来"
        )
