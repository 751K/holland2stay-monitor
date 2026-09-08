"""下单链路的常驻浏览器。

背景见 mcore/warm_browser.py 的模块文档。一句话：过 CF 是**浏览器**的成本
（中位 16.5s，两次有记录的下单失败全死在这），登录是**用户**的成本（~1.5s）。
把前者常驻下来，关键路径就只剩后者。

这里钉的多数是「借来的东西不能当自己的用」——那一类错误全都是静默的：关掉了
全进程共用的浏览器，下一个人拿到空壳，没有任何地方会报错。
"""
from __future__ import annotations

import threading
import time

import pytest

from mcore.warm_browser import WarmBrowserLane


class FakeFetcher:
    """够用的假浏览器：记下自己被关过没有、探针过没过。"""

    def __init__(self, *, alive: bool = True, probe_ok: bool = True) -> None:
        self._alive = alive
        self.probe_ok = probe_ok
        self.closed = 0
        self.probes = 0
        self.entered = 0

    def __enter__(self):
        self.entered += 1
        return self

    def is_alive(self) -> bool:
        return self._alive

    def probe_clearance(self) -> bool:
        self.probes += 1
        return self.probe_ok

    def close(self) -> None:
        self.closed += 1
        self._alive = False


@pytest.fixture
def lane(monkeypatch):
    """一条不会真开 Chromium 的常驻。"""
    ln = WarmBrowserLane()
    made: list[FakeFetcher] = []

    def _build(self=ln):
        f = FakeFetcher()
        made.append(f)
        self._fetcher = f
        self._born_at = self._last_io = time.monotonic()

    monkeypatch.setattr(ln, "_build", _build)
    ln.made = made          # type: ignore[attr-defined]
    return ln


class TestLifecycle:

    def test_it_comes_up_when_somebody_needs_it(self, lane):
        lane.heartbeat(wanted=True)
        assert lane.fetcher is not None
        assert lane.stats()["up"] is True

    def test_it_stays_down_when_nobody_uses_auto_book(self, lane):
        """没人开自动预订还留一个 Chromium，纯粹是白占内存 + 白给 H2S 发探针。"""
        lane.heartbeat(wanted=False)
        assert lane.fetcher is None
        assert lane.made == []

    def test_turning_auto_book_off_closes_it(self, lane):
        lane.heartbeat(wanted=True)
        f = lane.fetcher
        lane.heartbeat(wanted=False)
        assert lane.fetcher is None
        assert f.closed == 1

    def test_a_dead_browser_is_replaced(self, lane):
        """Chromium 被 OOM 干掉之后对象还在。看 is_alive，不看 is None。"""
        lane.heartbeat(wanted=True)
        first = lane.fetcher
        first._alive = False
        lane.heartbeat(wanted=True)
        assert lane.fetcher is not first
        assert lane.fetcher.is_alive()

    def test_it_is_replaced_before_it_ages_out(self, lane, monkeypatch):
        """到龄主动换，好让重建落在我们挑的时刻，而不是某次下单的中途。"""
        import mcore.warm_browser as m
        lane.heartbeat(wanted=True)
        first = lane.fetcher
        monkeypatch.setattr(m, "_MAX_AGE", -1.0)
        lane.heartbeat(wanted=True)
        assert lane.fetcher is not first

    def test_a_failed_build_backs_off_instead_of_hammering(self, monkeypatch):
        """CF 正在拒绝时反复重建只会加深怀疑，而这条常驻本来就不急。

        这条**走真的 _build**（不像别处那样把它换掉）——退避是 _build 里那行
        ``_next_rebuild_at`` 做的，替掉 _build 就等于替掉被测对象。
        """
        import browser_fetcher

        ln = WarmBrowserLane()
        attempts = []

        class Boom(FakeFetcher):
            def __init__(self, *a, **kw):
                super().__init__()
                attempts.append(1)

            def ensure_initialized(self):
                raise RuntimeError("CF clearance 25s 内未生效")

        monkeypatch.setattr(browser_fetcher, "BrowserFetcher", Boom)
        for _ in range(3):
            ln.heartbeat(wanted=True)
        assert ln.fetcher is None
        assert len(attempts) == 1, f"退避没生效，重建了 {len(attempts)} 次"

    def test_a_failed_build_retries_after_the_cooldown(self, monkeypatch):
        """退避是「先别急」，不是「从此不试」。"""
        import browser_fetcher
        import mcore.warm_browser as m

        ln = WarmBrowserLane()
        attempts = []

        class Boom(FakeFetcher):
            def __init__(self, *a, **kw):
                super().__init__()
                attempts.append(1)

            def ensure_initialized(self):
                raise RuntimeError("CF clearance 25s 内未生效")

        monkeypatch.setattr(browser_fetcher, "BrowserFetcher", Boom)
        monkeypatch.setattr(m, "_REBUILD_COOLDOWN", -1.0)
        for _ in range(3):
            ln.heartbeat(wanted=True)
        assert len(attempts) == 3

    def test_a_failed_build_closes_the_half_open_browser(self, monkeypatch):
        """挑战没过就把浏览器留着，是每次退避都漏一个 Chromium。"""
        import browser_fetcher

        ln = WarmBrowserLane()
        made = []

        class Boom(FakeFetcher):
            def __init__(self, *a, **kw):
                super().__init__()
                made.append(self)

            def ensure_initialized(self):
                raise RuntimeError("CF clearance 25s 内未生效")

        monkeypatch.setattr(browser_fetcher, "BrowserFetcher", Boom)
        ln.heartbeat(wanted=True)
        assert made and made[0].closed == 1


class TestKeepalive:
    """``h2s_clr`` 只有 0.5 小时，闲着不管这条常驻就退化成空壳。"""

    def test_idle_triggers_a_probe(self, lane, monkeypatch):
        import mcore.warm_browser as m
        lane.heartbeat(wanted=True)
        assert lane.fetcher.probes == 0
        monkeypatch.setattr(m, "_KEEPALIVE_INTERVAL", -1.0)
        lane.heartbeat(wanted=True)
        assert lane.fetcher.probes == 1

    def test_a_busy_lane_is_not_probed(self, lane):
        """一直在用的浏览器 cookie 由服务端续着，不需要额外探针。"""
        lane.heartbeat(wanted=True)
        lane.mark_used()
        lane.heartbeat(wanted=True)
        assert lane.fetcher.probes == 0

    def test_a_failing_probe_drops_the_browser(self, lane, monkeypatch):
        """探针不过 = 这条常驻已经不通了。

        留着它比没有更糟：下单会拿到一个看起来健康、实际每个请求都 403 的
        fetcher，而老路至少会自己重过挑战。
        """
        import mcore.warm_browser as m
        lane.heartbeat(wanted=True)
        f = lane.fetcher
        f.probe_ok = False
        monkeypatch.setattr(m, "_KEEPALIVE_INTERVAL", -1.0)
        monkeypatch.setattr(m, "_REBUILD_COOLDOWN", 9999.0)
        lane.heartbeat(wanted=True)
        assert lane.fetcher is None
        assert f.closed == 1


class TestSuppressionWindow:

    def test_it_does_not_build_while_cf_is_blocking_us(self, lane):
        lane.heartbeat(wanted=True, may_rebuild=False)
        assert lane.fetcher is None
        assert lane.made == []

    def test_but_a_healthy_one_keeps_being_kept_alive(self, lane, monkeypatch):
        """熔断期间它是唯一还通着的路，丢了就得等熔断结束再从头过挑战。"""
        import mcore.warm_browser as m
        lane.heartbeat(wanted=True)
        f = lane.fetcher
        monkeypatch.setattr(m, "_KEEPALIVE_INTERVAL", -1.0)
        lane.heartbeat(wanted=True, may_rebuild=False)
        assert lane.fetcher is f
        assert f.probes == 1


def _call_offset():
    r"""run_once 源码里「调用心跳」那一行的偏移；没有就是 None。

    用行首锚定的正则而不是裸子串：定义那一行前面有 ``def ``，不会命中。
    """
    import inspect
    import re

    import monitor
    m = re.search(r"^\s*_heartbeat_warm_browser\(", inspect.getsource(monitor.run_once),
                  re.MULTILINE)
    return m.start() if m else None


class TestWiring:
    """接线本身。

    上面每一条都可以在「心跳压根没被调用」的情况下全绿——那正是这次要防的形状：
    机制写好了、测好了，然后在生产里一次都不跑。
    """

    def test_the_round_loop_calls_the_heartbeat(self):
        """找的是**调用**，不是定义。

        第一版写的是 ``"_heartbeat_warm_browser(" in src``——而 ``def
        _heartbeat_warm_browser(...)`` 也含这个子串，于是把调用整行删掉，测试
        照样绿。接线测试匹配到定义上，就等于没测。
        """
        assert _call_offset() is not None, "run_once 里没有调用心跳"

    def test_the_heartbeat_runs_every_round_not_only_when_there_are_candidates(self):
        """它存在的意义就是在候选出现**之前**已经过了 CF。

        排在 _start_prewarm_for_candidates 后面、或者塞进只有候选才走的分支，
        就退回成 2026-09-08 之前那个每次现过挑战的样子。
        """
        import inspect

        import monitor
        src = inspect.getsource(monitor.run_once)
        hb = _call_offset()
        assert hb is not None
        pw = src.index("_start_prewarm_for_candidates(candidate_user_ids)")
        assert hb < pw, "心跳排在了预登录后面"

    def test_process_exit_closes_the_lane(self):
        """常驻不归 prewarm_cache 管（借出去的一律不许关它），退出要单独关一次。"""
        import inspect

        import monitor
        src = inspect.getsource(monitor._async_main)
        assert "warm_lane.close(" in src

    def test_suppression_window_blocks_rebuild(self):
        """熔断期间不新建——正在被拒绝时开浏览器只是再添几条失败记录。"""
        import inspect

        import monitor
        src = inspect.getsource(monitor.run_once)
        body = src[src.index("def _heartbeat_warm_browser"):]
        body = body[:body.index("def _start_prewarm_for_candidates")]
        assert "_h2s_login_suppressed_remaining()" in body
        assert "may_rebuild" in body

    def test_it_is_skipped_in_dry_run(self):
        import inspect

        import monitor
        src = inspect.getsource(monitor.run_once)
        body = src[src.index("def _heartbeat_warm_browser"):]
        body = body[:body.index("def _start_prewarm_for_candidates")]
        assert "if dry_run:" in body
