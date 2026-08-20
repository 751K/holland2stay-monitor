"""抓取熔断从「H2S 专属」推广到「每个 source 一个」。

问题
----
`monitor` 里 H2S 专属逻辑有 56 处引用：source 级熔断、canary 恢复探测、登录抑制、
专属 executor 线程。抽象层是对称的（``AbstractScraper`` + ``SCRAPER_REGISTRY``），
但编排层把 H2S 硬编码成了特例——这是历史遗留（H2S 曾是唯一 source）固化成的架构。

保护装在了最不需要它的那个 source 上。保留日志全量统计「整体抓取失败」次数：

    xior       RateLimitError     147 次   ← 无熔断
    ourdomain  ScrapeNetworkError  67 次   ← 无熔断
    ourcampus  ScrapeNetworkError  60 次   ← 无熔断
    xior       ScrapeNetworkError  57 次   ← 无熔断
    holland2stay 全部合计            6 次   ← 有熔断

Xior 的限流是**按 IP 累积**的（模块头写着 ~15–20 req/window），整源 429 之后没有
任何退避，下一轮照常再打——正是「限流最狠的时候接着撞」。

策略是按 source 配的，不是一刀切
--------------------------------
各家的失败语义不同，退避参数也就不该相同：

- H2S 的 403 要换出口 IP 才好，冷却长（30 分钟起，最长 6 小时）
- Xior 的 429 等一会就好，冷却短（10 分钟起，最长 1 小时），但**必须有**
- 429 是否该熔断，按 source 配：H2S 那边「等等就好」的判断没变，不能顺手改掉
"""
from __future__ import annotations

import pytest

from mcore.circuit import CircuitPolicy, SourceCircuits
from scrapers.base import BlockedError, RateLimitError, ScrapeNetworkError
from storage import Storage


@pytest.fixture
def st(tmp_path):
    s = Storage(tmp_path / "t.db", timezone_str="UTC")
    yield s
    s.close()


@pytest.fixture
def cs(st):
    c = SourceCircuits()
    c.load(st)
    return c


class TestEverySourceGetsOne:
    def test_unknown_source_falls_back_to_a_default_policy(self, cs):
        """新接一个平台不该顺带获得「没有熔断」这个默认值。"""
        p = cs.policy("some-new-platform")
        assert isinstance(p, CircuitPolicy)
        assert p.base_cooldown > 0 and p.max_cooldown >= p.base_cooldown

    @pytest.mark.parametrize("source", [
        "holland2stay", "xior", "ourdomain", "ourcampus",
    ])
    def test_all_registered_sources_have_a_circuit(self, source, cs, st):
        cs.trip(source, BlockedError("403"), storage=st)
        assert cs.remaining(source) > 0

    def test_circuits_are_independent(self, cs, st):
        """一个 source 熔断不能牵连另一个——这正是当初把 H2S 单拎出来的理由。"""
        cs.trip("xior", RateLimitError("429"), storage=st)
        assert cs.remaining("xior") > 0
        assert cs.remaining("holland2stay") == 0


class TestPolicyIsPerSource:
    def test_h2s_cooldown_is_unchanged(self, cs):
        """H2S 的参数一个字都不能变——这次是推广机制，不是调它的策略。"""
        import monitor

        p = cs.policy("holland2stay")
        assert p.base_cooldown == monitor._H2S_CIRCUIT_BASE_COOLDOWN
        assert p.max_cooldown == monitor._H2S_CIRCUIT_MAX_COOLDOWN

    def test_h2s_does_not_trip_on_rate_limit(self, cs, st):
        """H2S 那边「429 等等就好」的判断没变，不能顺手改掉。

        它的 429 由 scrapers 内部的 RATE_LIMIT_BACKOFF 退避处理，而且实测
        403 才是需要换 IP 的那个。
        """
        cs.trip("holland2stay", RateLimitError("429"), storage=st)
        assert cs.remaining("holland2stay") == 0

    def test_xior_trips_on_rate_limit(self, cs, st):
        """Xior 的 429 是按 IP 累积的，不退避就是接着撞——147 次实测。"""
        cs.trip("xior", RateLimitError("429"), storage=st)
        assert cs.remaining("xior") > 0

    def test_xior_cooldown_is_shorter_than_h2s(self, cs):
        """429 等一会就好，403 要换 IP。退避长度该反映这个差别。"""
        assert cs.policy("xior").base_cooldown < cs.policy("holland2stay").base_cooldown

    def test_network_errors_never_trip(self, cs, st):
        """网络失败已经有连续失败计数 + 冷却那条路，别叠两层退避。"""
        for source in ("holland2stay", "xior", "ourdomain"):
            cs.trip(source, ScrapeNetworkError("timeout"), storage=st)
            assert cs.remaining(source) == 0


class TestExponentialBackoffAndCanary:
    def test_cooldown_doubles_per_consecutive_trip(self, cs, st):
        got = [cs.trip("xior", RateLimitError("429"), storage=st) for _ in range(3)]
        assert got[1] == got[0] * 2 and got[2] == got[1] * 2

    def test_cooldown_is_capped(self, cs, st):
        for _ in range(20):
            last = cs.trip("xior", RateLimitError("429"), storage=st)
        assert last == cs.policy("xior").max_cooldown

    def test_open_then_canary_then_normal(self, cs, st):
        assert cs.plan("xior", n_tasks=4) == ("normal", 4)

        cs.trip("xior", RateLimitError("429"), storage=st)
        assert cs.plan("xior", n_tasks=4) == ("open", 0)

        cs.expire("xior", storage=st)          # 冷却到期，连败保留
        assert cs.plan("xior", n_tasks=4) == ("canary", 1)

        cs.recover("xior", storage=st)          # canary 成功
        assert cs.plan("xior", n_tasks=4) == ("normal", 4)

    def test_recovery_resets_the_streak(self, cs, st):
        for _ in range(3):
            cs.trip("xior", RateLimitError("429"), storage=st)
        cs.recover("xior", storage=st)
        first_again = cs.trip("xior", RateLimitError("429"), storage=st)
        assert first_again == cs.policy("xior").base_cooldown

    def test_no_tasks_means_nothing_to_plan(self, cs):
        assert cs.plan("xior", n_tasks=0) == ("none", 0)


class TestSurvivesRestart:
    def test_state_is_persisted_per_source(self, st):
        c1 = SourceCircuits(); c1.load(st)
        c1.trip("xior", RateLimitError("429"), storage=st)

        c2 = SourceCircuits(); c2.load(st)
        assert c2.remaining("xior") > 0, (
            "熔断状态没跨重启——部署一次就把退避清零，正是限流最狠的时候"
        )
        assert c2.remaining("holland2stay") == 0


class TestNeverBlocksStartup:
    """退避状态是**优化**，不是抓取的前提。读不出来最多退化成「这一轮不退避」，
    绝不该让它决定进程能不能起来。

    ``load()`` 的 docstring 一直写着「绝不抛」，但只有 meta 内容损坏被
    ``_as_float`` 兜住了；连接本身出问题（库被关掉、锁死、schema 损坏）会一路
    抛出去。迁移过程中真的踩到：测试关掉 Storage 之后 teardown 再碰熔断，
    10 个用例集体 ProgrammingError。
    """

    def test_closed_database_does_not_raise(self, tmp_path):
        from mcore.backoff import PersistedBackoff

        s = Storage(tmp_path / "t.db", timezone_str="UTC")
        s.close()
        b = PersistedBackoff("k", max_seconds=600)
        b.load(s)                      # 不抛
        assert b.remaining() == 0

    def test_circuits_load_survives_a_broken_storage(self, tmp_path):
        s = Storage(tmp_path / "t.db", timezone_str="UTC")
        s.close()
        c = SourceCircuits()
        c.load(s)                      # 不抛
        assert c.remaining("xior") == 0

    def test_trip_on_broken_storage_still_works_in_memory(self, tmp_path):
        """落库失败时熔断仍然生效——只是不跨重启，比完全不熔断强。"""
        s = Storage(tmp_path / "t.db", timezone_str="UTC")
        c = SourceCircuits()
        c.load(s)
        s.close()
        assert c.trip("xior", RateLimitError("429"), storage=s) > 0
        assert c.remaining("xior") > 0


class TestMonitorWiring:
    """守卫：monitor 必须真的用上 per-source 熔断，而不是留着 H2S 专属那套。"""

    def test_monitor_holds_source_circuits(self):
        import monitor

        assert isinstance(monitor._source_circuits, SourceCircuits)

    def test_h2s_only_globals_are_gone(self):
        import monitor

        assert not hasattr(monitor, "_h2s_circuit"), (
            "H2S 专属熔断器又回来了——机制会重新分叉成两套"
        )

    def test_non_h2s_failures_trip_a_circuit(self):
        """源码级守卫：通用调度路径必须调 trip()。

        少了这一行，非 H2S 的 source 就还是「失败了下一轮原速再来」——
        xior 整源 429 失败 147 次正是这么攒出来的。
        """
        import ast
        import inspect
        import pathlib

        src = pathlib.Path("monitor.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_dispatch_isolated":
                body = ast.unparse(node)
                assert "_source_circuits.trip" in body, (
                    "通用调度路径没有打开熔断，非 H2S source 依然裸奔"
                )
                assert "_source_circuits.recover" in body, (
                    "canary 成功后没有关闭熔断，会一直停在 canary 模式"
                )
                return
        raise AssertionError("找不到 _dispatch_isolated，这条守卫已失效")

    def test_h2s_still_suppresses_login_on_block(self):
        """反向守卫：熔断推广了，但 H2S 独有的登录抑制不能跟着丢。

        只有 H2S 有自动预订，403 之后继续碰登录接口只会让 WAF 状态更热。
        """
        import inspect

        import monitor

        src = inspect.getsource(monitor._mark_h2s_scrape_blocked)
        assert "_mark_h2s_login_blocked" in src
