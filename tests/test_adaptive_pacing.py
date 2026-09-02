"""按 429 历史自动伸缩最小抓取间隔的测试。

背景
----
`_DEFAULT_SOURCE_MIN_INTERVALS = {"xior": 180}` 是手工自上而下试出来的，
config.py 的注释写着「600 → 300 都干净，现在试 180……若 180 也干净，下一档再
往 120 试」。

而 180 并不干净——生产按 task 计的 429：08-18 共 72 次、08-19 共 80 次、
08-20 共 39 次。这套调法默认「不干净会有人注意到并退回上一档」，没有人退。

熔断解决不了这件事：它管「出事之后停多久」，冷却一结束节奏就回到 180 秒，
过三五十分钟再撞一次。每天约 10 次，从 08-10 起从没停过。

降档为什么按时间而不是按轮数
----------------------------
第一版写的是「连续 8 轮干净才降一档」，上线一天就被实测推翻：

    08-21 08:52   ×2 → ×1   攒够 8 轮
    08-21 09:14   ×1 → ×2   22 分钟后就撞回去

同一个轮数在不同时段意味着完全不同的耐心：峰内 20 秒一轮，8 轮只有 2 分钟；
峰外 300 秒一轮，8 轮是 40 分钟。而限流恢复只跟真实时间有关。
"""
from __future__ import annotations

import pytest

from mcore.pacing import AdaptivePacing
from scrapers.base import BlockedError, RateLimitError, ScrapeNetworkError
from storage import Storage


@pytest.fixture
def st(tmp_path):
    s = Storage(tmp_path / "t.db")
    yield s
    s.close()


#: 测试用的时间锚。判据带时间维度，用例必须把 now 钉死。
_T = 1_700_000_000.0


def _p(**kw):
    return AdaptivePacing(**kw)


# ── 倍率本身 ────────────────────────────────────────────────────


class TestMultiplierLadder:
    def test_starts_at_base(self):
        assert _p().gap_for("xior", 180) == 180

    def test_doubles_on_rate_limit(self):
        p = _p()
        p.penalize("xior")
        assert p.gap_for("xior", 360) == 720     # 峰内档
        assert p.gap_for("xior", 180) == 360     # 峰外档

    def test_caps_at_two(self):
        """上限取 2 是为了**不误伤峰外**。

        倍率乘的是当前时段那一档。峰外基准 180，×2 = 360 仍低于峰外自然节奏
        381 秒，闸门保持不生效；若允许 ×4，一次峰内的 429 会把峰外一起压到
        720 秒——而峰外每轮 429 概率只有 0.09%，本来就不需要退避。
        """
        p = _p()
        for _ in range(10):
            p.penalize("xior")
        assert p.multiplier("xior") == 2.0
        assert p.gap_for("xior", 360) == 720     # 峰内封顶
        assert p.gap_for("xior", 180) == 360     # 峰外仍在 381 之下

    def test_penalize_reports_whether_it_moved(self):
        p = _p()
        assert p.penalize("xior") is True
        assert p.penalize("xior") is False      # 已封顶

    def test_relax_needs_elapsed_time_not_round_count(self):
        """跑一万轮也不降档，时间不到就是不到。"""
        p = _p(calm_seconds=14400)
        t = _T
        p.penalize("xior", now=t)
        for i in range(10_000):
            assert p.relax("xior", now=t + i * 0.1) is False
        assert p.multiplier("xior") == 2.0
        assert p.relax("xior", now=t + 14400) is True
        assert p.multiplier("xior") == 1.0

    def test_the_regression_that_motivated_this(self):
        """复现 08-21 那次振荡：降档 22 分钟后又被限流。

        旧实现（按轮数）在峰内 20 秒一轮时，8 轮 = 2 分钟就降档；按时间之后，
        22 分钟远不够 4 小时，倍率原地不动。
        """
        p = _p(calm_seconds=14400)
        t = _T
        p.penalize("xior", now=t)                       # 09:14 那次之前
        # 峰内 20 秒一轮，跑满 22 分钟 = 66 轮
        for i in range(66):
            assert p.relax("xior", now=t + i * 20) is False
        assert p.multiplier("xior") == 2.0, "22 分钟就降档 = 又回到会振荡的实现"

    def test_penalty_restarts_the_clock(self):
        p = _p(calm_seconds=14400)
        t = _T
        p.penalize("xior", now=t)
        assert p.relax("xior", now=t + 14000) is False   # 快到了
        p.penalize("xior", now=t + 14000)                # 又撞一次，重新起算
        assert p.relax("xior", now=t + 14000 + 14000) is False
        assert p.relax("xior", now=t + 14000 + 14400) is True

    def test_capped_penalty_still_restarts_the_clock(self):
        """封顶不等于风险消失——反而说明还在挨打，不该继续朝降档爬。"""
        p = _p(calm_seconds=14400)
        t = _T
        p.penalize("xior", now=t)                        # ×2，已封顶
        assert p.penalize("xior", now=t + 10000) is False  # 倍率不变
        assert p.relax("xior", now=t + 14400) is False     # 但计时被重置了
        assert p.relax("xior", now=t + 10000 + 14400) is True

    def test_relax_at_base_is_a_noop(self):
        p = _p()
        for i in range(50):
            assert p.relax("xior", now=_T + i * 100000) is False
        assert p.multiplier("xior") == 1.0

    def test_clock_going_backwards_keeps_the_backoff(self):
        """时钟回拨时保守一侧是**保持退避**，不是当场降档。"""
        p = _p(calm_seconds=14400)
        t = _T
        p.penalize("xior", now=t)
        assert p.relax("xior", now=t - 100000) is False
        assert p.multiplier("xior") == 2.0
        # 回拨之后重新起算
        assert p.relax("xior", now=t - 100000 + 14400) is True

    def test_sources_are_independent(self):
        p = _p()
        p.penalize("xior")
        assert p.gap_for("xior", 180) == 360
        assert p.gap_for("ourdomain", 120) == 120


# ── 与配置的关系 ────────────────────────────────────────────────


class TestRespectsConfiguration:
    def test_throttle_off_stays_off(self):
        """用户显式写 xior:0 关掉节流，自适应不许偷偷把它打开。"""
        p = _p()
        for _ in range(5):
            p.penalize("xior")
        assert p.gap_for("xior", 0) == 0
        assert p.gap_for("xior", -1) == 0

    def test_multiplier_scales_whatever_base_is_configured(self):
        p = _p()
        p.penalize("xior")
        assert p.gap_for("xior", 300) == 600


# ── 落库 ────────────────────────────────────────────────────────


class TestPersistence:
    def test_survives_a_restart(self, st):
        """部署往往就发生在出问题的时候——倍率只活在进程内等于每次部署清零。"""
        p = _p()
        p.penalize("xior", storage=st, now=_T)

        revived = _p()
        revived.load(_seeded(st))
        assert revived.gap_for("xior", 360) == 720

    def test_the_clock_survives_too(self, st):
        """只存倍率不存计时起点的话，频繁重启会让它永远攒不满、只升不降。"""
        p = _p(calm_seconds=14400)
        p.penalize("xior", storage=st, now=_T)

        revived = _p(calm_seconds=14400)
        revived.load(_seeded(st))
        assert revived.relax("xior", storage=st, now=_T + 14000) is False
        assert revived.relax("xior", storage=st, now=_T + 14400) is True
        assert revived.multiplier("xior") == 1.0

    def test_lost_clock_restarts_instead_of_dropping(self, st):
        """倍率恢复了但计时起点丢了（旧版本升上来 / meta 写坏）：

        必须从现在开始重新计时，而不是当作「已经干净很久」立刻降档。旧键
        pacing_calm_ 存的是轮数，当成时间戳读会算出「干净了 56 年」。
        """
        st.set_meta("pacing_mult_xior", "2.0")
        st.set_meta("pacing_calm_xior", "7")        # 旧格式的残留
        p = _p(calm_seconds=14400)
        p.load(_seeded(st))
        assert p.multiplier("xior") == 2.0
        assert p.relax("xior", storage=st, now=_T) is False        # 不许立刻降
        assert p.relax("xior", storage=st, now=_T + 14400) is True # 从这一刻起算

    def test_load_never_throws_on_a_dead_storage(self, tmp_path):
        s = Storage(tmp_path / "t.db")
        s.close()
        p = _p()
        p.load(s)                       # 不许抛：节奏是优化，不是抓取的前提
        assert p.multiplier("xior") == 1.0

    def test_corrupt_meta_falls_back_to_base(self, st):
        st.set_meta("pacing_mult_xior", "not-a-number")
        p = _p()
        p.load(st)
        assert p.multiplier("xior") == 1.0

    def test_reset_returns_to_base(self, st):
        p = _p()
        p.penalize("xior", storage=st, now=_T)
        p.reset("xior", storage=st)
        revived = _p()
        revived.load(_seeded(st))
        assert revived.gap_for("xior", 360) == 360


def _seeded(st):
    """load() 从**注册表**取 source，库里不需要预先有遥测行。

    这正是 _known() 不用 round_stats_sources() 的理由：需要恢复倍率的那个
    source，往往恰好是被限流拖到还没写出遥测行的那个。
    """
    return st


# ── monitor 接线 ────────────────────────────────────────────────


class TestMonitorWiring:
    """判定逻辑对了，接线断了照样等于没做——项目在这上面栽过两次。"""

    # 不需要自己复位：conftest 的 _reset_monitor_h2s_guards 是 autouse 的，
    # 每个用例前后都会把 _source_pacing 连同熔断器一起复位。

    def _cfg(self, **kw):
        class _C:
            source_min_intervals = kw.get("intervals", {"xior": 180})
        return _C()

    def _tasks(self, n=2):
        from scrapers.base import ScrapeTask
        return [ScrapeTask(source="xior", city_key=str(i), city_display=f"C{i}")
                for i in range(n)]

    def test_interval_gate_uses_the_multiplier(self, st):
        import monitor
        tasks = self._tasks()
        cfg = self._cfg()

        # t=0 放行并记下时刻
        assert monitor._apply_source_intervals(tasks, cfg, st, now=0.0) == tasks
        # t=200 > 基准 180，本该放行
        assert monitor._apply_source_intervals(tasks, cfg, st, now=200.0) == tasks

        monitor._source_pacing.reset("xior")
        assert monitor._apply_source_intervals(tasks, cfg, st, now=1000.0) == tasks
        monitor._source_pacing.penalize("xior")          # ×2 → 360 秒
        assert monitor._apply_source_intervals(tasks, cfg, st, now=1200.0) == []
        assert monitor._apply_source_intervals(tasks, cfg, st, now=1400.0) == tasks

    def test_throttle_off_is_still_off_under_penalty(self, st):
        import monitor
        cfg = self._cfg(intervals={"xior": 0})
        monitor._source_pacing.penalize("xior")
        monitor._source_pacing.penalize("xior")
        tasks = self._tasks()
        for t in (0.0, 1.0, 2.0):
            assert monitor._apply_source_intervals(tasks, cfg, st, now=t) == tasks

    def test_bind_persistent_state_loads_pacing(self):
        """启动不 bind 的话，倍率每次部署清零——单元测试自己传 storage，
        接线断了照样全绿。用 AST 钉住。"""
        import ast
        import inspect
        import monitor

        fn = inspect.getsource(monitor._bind_persistent_state)
        calls = [
            n for n in ast.walk(ast.parse(fn.strip()))
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "load"
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "_source_pacing"
        ]
        assert calls, "_bind_persistent_state 必须调 _source_pacing.load(storage)"

    def test_only_rate_limit_penalizes(self):
        """403 / 网络错误 / 维护都不是「打得太勤」造成的，拉长间隔治不了它们。"""
        import ast
        import inspect
        import monitor

        src = inspect.getsource(monitor)
        tree = ast.parse(src)
        guards = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.If) and isinstance(node.test, ast.Call)):
                continue
            if getattr(node.test.func, "id", "") != "isinstance":
                continue
            body = ast.dump(ast.Module(body=node.body, type_ignores=[]))
            if "penalize" not in body:
                continue
            guards.append(ast.dump(node.test))
        assert guards, "penalize 必须包在 isinstance 判断里"
        for g in guards:
            assert "RateLimitError" in g, f"penalize 的判据不是 429: {g}"
            for other in ("BlockedError", "ScrapeNetworkError",
                          "UpstreamMaintenanceError"):
                assert other not in g, f"penalize 不该对 {other} 生效"

    def test_success_path_relaxes(self):
        """成功一轮必须喂给 relax，否则倍率只升不降——撞过一次 429 之后
        Xior 就永远停在 720 秒，房源出现要等 12 分钟才发现。

        单元测试直接调 relax()，接线删掉照样全绿；用 AST 钉住它确实长在
        「这一轮成功了」的记账旁边。
        """
        import ast
        import inspect
        import monitor

        tree = ast.parse(inspect.getsource(monitor))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = ast.dump(node)
            # 记「本轮这个 source 成功了」的那个函数
            if "succeeded_sources" not in body or "append" not in body:
                continue
            if "_source_pacing" in body and "relax" in body:
                return
        pytest.fail(
            "没有任何一个记录 succeeded_sources 的函数调用 _source_pacing.relax()"
            "——倍率将只升不降"
        )

    # ── 逐处检查，不是「有一处就算过」 ───────────────────────────────
    #
    # 上面两条用例都是「找到一处合格的就通过」：test_success_path_relaxes 遍历函数、
    # 命中第一个含 relax 的就 return；test_only_rate_limit_penalizes 只检查已有的
    # penalize 守卫对不对，不管有没有漏掉某个分支。
    #
    # run_once 里这两件事各写了两份——跨源隔离的 _dispatch_isolated 一份，
    # Holland2Stay 走独立熔断路径又一份。于是 **H2S 从 2026-08-17 起既不 penalize
    # 也不 relax，倍率恒为 1.0，自适应节奏对它整个不生效**，而这组用例全绿。
    #
    # 下面两条按「每一处」检查。同类的接线守卫见
    # tests/test_source_isolation_reports_proxy.py 的 TestWiredIntoTheIsolationBranch。

    @staticmethod
    def _sites(marker: str) -> list[str]:
        import inspect
        import re
        import monitor
        src = inspect.getsource(monitor.run_once)
        return [src[m.start():m.start() + 1600]
                for m in re.finditer(re.escape(marker), src)]

    def test_more_than_one_site_of_each(self):
        """前提本身要钉住：合并成一处时，下面的「每一处」就该改写。"""
        assert len(self._sites("succeeded_sources.append(")) >= 2
        assert len(self._sites("source_failures.append(")) >= 2

    def test_every_success_site_relaxes(self):
        for i, block in enumerate(self._sites("succeeded_sources.append(")):
            assert "_source_pacing.relax" in block, (
                f"第 {i + 1} 处「本轮成功」没有 relax——该 source 的倍率只升不降")

    def test_every_failure_site_penalizes(self):
        for i, block in enumerate(self._sites("source_failures.append(")):
            assert "_source_pacing.penalize" in block, (
                f"第 {i + 1} 处「整源失败」没有 penalize——该 source 撞 429 也不会退让")
            assert "RateLimitError" in block, (
                f"第 {i + 1} 处的 penalize 没有 429 守卫")


class TestShippedDefaults:
    """出厂参数就是生产行为，必须钉住。

    所有衰减用例都显式传 calm_seconds，把默认值改成 2 分钟照样全绿——而 2 分钟
    正是 08-21 那次振荡的量级。
    """

    def test_defaults_match_the_measurements(self):
        p = AdaptivePacing()
        assert p._calm_seconds == 14400.0, "衰减窗口 4 小时"
        assert p._max == 2.0, "上限 ×2：峰内封顶 720 秒，峰外 360 仍不生效"
        assert p._factor == 2.0

    def test_default_window_would_have_stopped_the_regression(self):
        """08-21 09:14 那次：降档 22 分钟后又被限流。出厂参数下不会降。"""
        p = AdaptivePacing()
        p.penalize("xior", now=_T)
        assert p.relax("xior", now=_T + 22 * 60) is False
        assert p.multiplier("xior") == 2.0

    def test_default_ceiling_leaves_off_peak_alone(self):
        """峰内封顶 720（> 实测干净的 381），峰外封顶 360（< 381，闸门不生效）。"""
        p = AdaptivePacing()
        for _ in range(5):
            p.penalize("xior", now=_T)
        assert p.gap_for("xior", 360) == 720
        assert p.gap_for("xior", 180) == 360


class TestMultiStepLadder:
    """上限配得更高时，降档要一档一档走，每档重新计时。

    出厂 max=2 时只有两档，走不到中间态；这组用例专门覆盖那个分支，否则它是
    死代码，改坏了没人知道。
    """

    def test_each_step_restarts_the_clock(self):
        p = _p(max_multiplier=4.0, calm_seconds=3600)
        t = _T
        p.penalize("xior", now=t)
        p.penalize("xior", now=t)
        assert p.multiplier("xior") == 4.0

        assert p.relax("xior", now=t + 3600) is True      # ×4 → ×2
        assert p.multiplier("xior") == 2.0
        # 关键：这一档要重新计时，不能凭上一档的时长直接再降
        assert p.relax("xior", now=t + 3600 + 3599) is False
        assert p.multiplier("xior") == 2.0
        assert p.relax("xior", now=t + 3600 + 3600) is True
        assert p.multiplier("xior") == 1.0

    def test_reaching_base_clears_the_clock(self):
        p = _p(max_multiplier=4.0, calm_seconds=3600)
        t = _T
        p.penalize("xior", now=t)
        p.relax("xior", now=t + 3600)                      # 回到 ×1
        assert p.multiplier("xior") == 1.0
        for i in range(20):
            assert p.relax("xior", now=t + 3600 + i * 100000) is False
