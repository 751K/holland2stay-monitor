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


def _p(**kw):
    return AdaptivePacing(**kw)


# ── 倍率本身 ────────────────────────────────────────────────────


class TestMultiplierLadder:
    def test_starts_at_base(self):
        assert _p().gap_for("xior", 180) == 180

    def test_doubles_on_rate_limit(self):
        p = _p()
        p.penalize("xior")
        assert p.gap_for("xior", 180) == 360
        p.penalize("xior")
        assert p.gap_for("xior", 180) == 720

    def test_caps_at_max(self):
        """360 与 720 恰好夹住注释里实测干净的 300 与 600，两次碰撞即收敛。"""
        p = _p()
        for _ in range(10):
            p.penalize("xior")
        assert p.gap_for("xior", 180) == 720
        assert p.multiplier("xior") == 4.0

    def test_penalize_reports_whether_it_moved(self):
        p = _p()
        assert p.penalize("xior") is True
        assert p.penalize("xior") is True
        assert p.penalize("xior") is False      # 已封顶

    def test_relax_needs_a_full_calm_streak(self):
        p = _p(calm_rounds=8)
        p.penalize("xior")                      # ×2
        for i in range(7):
            assert p.relax("xior") is False
            assert p.multiplier("xior") == 2.0
        assert p.relax("xior") is True
        assert p.multiplier("xior") == 1.0

    def test_penalty_resets_the_calm_streak(self):
        """攒到一半又撞一次，得从头攒——否则会在阈值附近来回振荡。"""
        p = _p(calm_rounds=8)
        p.penalize("xior")
        for _ in range(7):
            p.relax("xior")
        p.penalize("xior")                      # 计数清零，倍率再翻
        for _ in range(7):
            assert p.relax("xior") is False
        assert p.multiplier("xior") == 4.0

    def test_relax_at_base_is_a_noop(self):
        p = _p()
        for _ in range(50):
            assert p.relax("xior") is False
        assert p.multiplier("xior") == 1.0

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
        p.penalize("xior", storage=st)
        p.penalize("xior", storage=st)

        revived = _p()
        revived.load(_seeded(st))
        assert revived.gap_for("xior", 180) == 720

    def test_calm_streak_survives_too(self, st):
        """只存倍率不存计数的话，频繁重启会让它永远攒不满、只升不降。"""
        p = _p(calm_rounds=8)
        p.penalize("xior", storage=st)
        for _ in range(7):
            p.relax("xior", storage=st)

        revived = _p(calm_rounds=8)
        revived.load(_seeded(st))
        assert revived.relax("xior", storage=st) is True     # 第 8 轮就该降
        assert revived.multiplier("xior") == 1.0

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
        p.penalize("xior", storage=st)
        p.reset("xior", storage=st)
        revived = _p()
        revived.load(_seeded(st))
        assert revived.gap_for("xior", 180) == 180


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
