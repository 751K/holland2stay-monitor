"""退避 / 熔断 / 告警节流的状态必须跨进程重启存活。

问题
----
这些状态全是 monitor 的模块级全局，重启即清零：

    _h2s_circuit_open_until / _fail_streak      H2S 熔断（最长 6 小时退避）
    _h2s_login_blocked_until                    登录抑制（1 小时）
    blocked_fail_streak                         403 指数退避（最长 2 小时）
    _last_*_notify_at ×6                        全部告警节流

也就是说：**正在被 CF 封、退避已经拉到 2 小时的时候，部署一次，立刻满速重打。**
2026-08-20 一天之内部署了 12 次 = 12 次退避清零。

而这个判断项目自己写下过。``_apply_source_intervals`` 里就有：

    时间戳存 meta，重启后仍然生效——否则频繁重启会绕过节流，
    **正是限流最狠的时候**（重启往往就是因为出问题了）。

同一个判断、同一个文件，只落地在了节流上，没落地到熔断和退避。

为什么必须换成墙钟
------------------
这些状态原本用 ``time.monotonic()``。monotonic 的零点是**进程启动**，跨重启
持久化它毫无意义（存 5000 秒，重启后读出来还是 5000，而 now 变成了 3）。
所以持久化必须换 ``time.time()``，代价是要处理时钟跳变——见 clamp 的用例。
"""
from __future__ import annotations

import pytest

from mcore.backoff import PersistedBackoff
from mcore.circuit import SourceCircuits
from storage import Storage


@pytest.fixture
def st(tmp_path):
    s = Storage(tmp_path / "t.db", timezone_str="UTC")
    yield s
    s.close()


class TestSurvivesRestart:
    def test_deadline_outlives_the_process(self, st):
        b1 = PersistedBackoff("h2s_circuit", max_seconds=21600)
        b1.load(st)
        b1.open(1800, reason="403", storage=st)
        assert 1700 < b1.remaining() <= 1800

        # 「进程重启」：全新对象，只有库里的东西
        b2 = PersistedBackoff("h2s_circuit", max_seconds=21600)
        b2.load(st)
        assert 1700 < b2.remaining() <= 1800, (
            "重启后退避清零——正在被封的时候部署一次就满速重打"
        )
        assert b2.reason == "403"

    def test_fail_streak_outlives_the_process(self, st):
        """连败计数也要活下来，否则指数退避每次重启都从第一级重新爬。"""
        b1 = PersistedBackoff("h2s_circuit", max_seconds=21600)
        b1.load(st)
        for _ in range(4):
            b1.bump()
        b1.open(600, reason="x", storage=st)

        b2 = PersistedBackoff("h2s_circuit", max_seconds=21600)
        b2.load(st)
        assert b2.fail_streak == 4

    def test_reset_clears_persisted_state(self, st):
        b1 = PersistedBackoff("h2s_circuit", max_seconds=21600)
        b1.load(st)
        b1.bump(); b1.open(600, reason="x", storage=st)
        b1.reset(st)

        b2 = PersistedBackoff("h2s_circuit", max_seconds=21600)
        b2.load(st)
        assert b2.remaining() == 0
        assert b2.fail_streak == 0

    def test_expired_deadline_reads_as_zero(self, st):
        b = PersistedBackoff("k", max_seconds=3600)
        b.load(st)
        b.open(-5, reason="过期", storage=st)
        assert b.remaining() == 0


class TestClockJumpIsClamped:
    """墙钟是持久化的代价：NTP 往回跳会让 deadline 看起来远在未来。

    钳到配置上限是安全方向——**永远不会等得比配置的最长退避还久**。不钳的话
    一次时钟跳变能让 H2S 停摆到天荒地老，而且没有任何日志说得清为什么。
    """

    def test_remaining_never_exceeds_the_configured_max(self, st):
        b = PersistedBackoff("k", max_seconds=600)
        b.load(st)
        b.open(600, reason="x", storage=st)
        # 模拟时钟往回跳 10 天：库里的 deadline 变成了「10 天后」
        st.set_meta("backoff:k:until", str(float(b._until) + 86400 * 10))
        b2 = PersistedBackoff("k", max_seconds=600)
        b2.load(st)
        assert b2.remaining() <= 600, (
            f"时钟跳变后退避被放大到 {b2.remaining()}s，超过配置上限 600s"
        )

    def test_corrupt_meta_is_ignored_not_fatal(self, st):
        st.set_meta("backoff:k:until", "这不是数字")
        st.set_meta("backoff:k:streak", "也不是")
        b = PersistedBackoff("k", max_seconds=600)
        b.load(st)                      # 不抛
        assert b.remaining() == 0
        assert b.fail_streak == 0


class TestThrottleAlsoPersists:
    """告警节流同理：部署一次就把 30 分钟的节流窗口清零，用户重复收到同一条。"""

    def test_throttle_window_survives_restart(self, st):
        b1 = PersistedBackoff("notify_block", max_seconds=1800)
        b1.load(st)
        assert b1.claim(1800, storage=st) is True      # 首次放行
        assert b1.claim(1800, storage=st) is False     # 窗口内拒绝

        b2 = PersistedBackoff("notify_block", max_seconds=1800)
        b2.load(st)
        assert b2.claim(1800, storage=st) is False, (
            "重启后节流窗口清零——部署一次用户就重复收到同一条告警"
        )


class TestMonitorUsesIt:
    """守卫：monitor 里这几个状态不能再退回裸的模块级 float。"""

    def test_h2s_circuit_state_is_persisted(self):
        import monitor

        assert isinstance(monitor._source_circuits, SourceCircuits)
        assert isinstance(monitor._h2s_login_block, PersistedBackoff)

    def test_no_monotonic_backed_circuit_globals_remain(self):
        import monitor

        for name in ("_h2s_circuit_open_until", "_h2s_circuit_fail_streak",
                     "_h2s_login_blocked_until"):
            assert not hasattr(monitor, name), (
                f"monitor.{name} 又变回进程内全局了——部署会重置退避"
            )


class TestExpireVsReset:
    """``expire()`` 和 ``reset()`` 的区别就是连败计数，别混。

    ``reset`` = 问题解决了（canary 成功）→ 指数退避从头开始。
    ``expire`` = 这一轮的等待到点了 → 下次还失败的话，退避要接着往上爬。

    混掉的后果：持续被封时每次退避都从 30 分钟重新起步，永远爬不到 6 小时的
    上限——而那个上限正是用来保护出口 IP 的。
    """

    def test_expire_keeps_the_streak(self, st):
        b = PersistedBackoff("k", max_seconds=600)
        b.load(st)
        b.bump(); b.bump()
        b.open(600, reason="x", storage=st)
        b.expire(st)
        assert b.remaining() == 0
        assert b.fail_streak == 2, "expire 把连败计数也清了，指数退避会永远从头爬"

    def test_reset_clears_the_streak(self, st):
        b = PersistedBackoff("k", max_seconds=600)
        b.load(st)
        b.bump(); b.bump()
        b.reset(st)
        assert b.fail_streak == 0

    def test_expire_persists(self, st):
        b = PersistedBackoff("k", max_seconds=600)
        b.load(st); b.bump(); b.open(600, storage=st); b.expire(st)
        b2 = PersistedBackoff("k", max_seconds=600)
        b2.load(st)
        assert b2.remaining() == 0 and b2.fail_streak == 1


class TestStartupActuallyWiresIt:
    """守卫：启动路径必须真的把落库句柄接上并恢复状态。

    这是整套机制在生产生效的唯一接线点。少了它，``PersistedBackoff`` 全都拿不到
    storage，``claim`` / ``open`` 静默退化成进程内状态——**和修之前一模一样**，
    而且所有单元测试照样通过（它们自己传 storage）。

    第一轮变异测试正是这么漏掉的：把 ``_bind_persistent_state(storage)`` 从
    ``_async_main`` 里删掉，12 条用例全绿。
    """

    def test_async_main_binds_persistent_state(self):
        import ast
        import pathlib

        tree = ast.parse(pathlib.Path("monitor.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name != "_async_main":
                continue
            called = {
                n.func.id for n in ast.walk(node)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            }
            assert "_bind_persistent_state" in called, (
                "启动时没有恢复退避状态——所有退避/熔断/节流会静默退回进程内"
            )
            return
        raise AssertionError("找不到 _async_main，这条守卫已失效")

    def test_bind_restores_every_registered_backoff(self, st):
        """所有实例都要被 load，漏一个那一项就永远从零开始。"""
        import monitor

        st.set_meta("backoff:circuit_holland2stay:until", str(9e18))
        st.set_meta("backoff:throttle_notify_block:until", str(9e18))
        try:
            monitor._bind_persistent_state(st)
            assert monitor._source_circuits.remaining("holland2stay") > 0
            assert monitor._throttle_notify_block.remaining() > 0
        finally:
            monitor._source_circuits.recover("holland2stay")
            monitor._throttle_notify_block.reset()
            monitor._throttle_storage_ref = None
