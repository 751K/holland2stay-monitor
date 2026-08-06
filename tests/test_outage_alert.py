"""全面故障（所有 source 同时失败）必须告警。

2026-08-05 04:24–09:29，代理断了 5 小时 5 分钟，59 轮全部 source 失败，admin 一条
告警都没收到。原因不是判定写错了，而是**根本没有判定**：main_loop 的 network /
blocked / 未预期异常三个 except 分支只 ``logger.error``。而 watchdog 只在跑完一轮
之后才评估，run_once 上抛时那一轮的 watchdog 压根不执行。

于是形成了一个反向盲区——某个 source 挂了会告警，全部挂了反而静默。

这个 bug 有两层，两层都要守：

1. **行为层**：``_OutageTracker`` 的节流与恢复判定（下面第一组）。
2. **接线层**：每个「全面故障」分支都真的调了它（下面第二组）。第二层才是当初
   失效的那一层——判定逻辑写得再好，没接上就等于不存在。同理，
   ``_dispatch_watchdog_alerts`` 的注释当时写着「main_loop 已经在告警了」，
   而那句话是假的；文档不能替代测试。
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

import monitor

MONITOR_PY = Path(monitor.__file__)


# ── 第一组：节流与恢复判定 ──────────────────────────────────────────


@pytest.fixture
def tracker():
    return monitor._OutageTracker()


class TestOutageTracker:
    def test_first_failure_alerts_immediately(self, tracker):
        """全面宕机不该「先观察几轮」——门槛已经在调用方把住了。"""
        assert tracker.record_failure(0.0) is True

    def test_repeat_within_backoff_is_silent(self, tracker):
        tracker.record_failure(0.0)
        assert tracker.record_failure(60.0) is False
        assert tracker.record_failure(899.0) is False

    def test_backoff_grows_then_caps(self, tracker):
        """15 → 30 → 60 分钟后封顶，5 小时故障不该收 20 条。"""
        t = 0.0
        tracker.record_failure(t)
        fired = []
        for step in (900, 1800, 3600, 3600):
            t += step
            fired.append(tracker.record_failure(t))
            # 刚发过，紧接着一轮必须沉默
            assert tracker.record_failure(t + 1) is False
        assert fired == [True, True, True, True]

    def test_five_hour_outage_sends_a_handful_not_a_flood(self, tracker):
        """按昨晚的真实形态回放：5 小时、每 5 分钟一轮。"""
        alerts = sum(tracker.record_failure(i * 300.0) for i in range(61))
        assert tracker.rounds == 61
        assert 4 <= alerts <= 8, f"5 小时发了 {alerts} 条"

    def test_tracks_duration_and_rounds(self, tracker):
        for i in range(3):
            tracker.record_failure(i * 300.0)
        assert tracker.rounds == 3
        assert tracker.elapsed(1800.0) == 1800.0

    def test_recovery_reports_span_and_rounds(self, tracker):
        tracker.record_failure(0.0)
        tracker.record_failure(300.0)
        assert tracker.record_success(18300.0) == (18300.0, 2)

    def test_recovery_fires_once(self, tracker):
        """恢复后每轮都成功，不能每轮都播报一次「已恢复」。"""
        tracker.record_failure(0.0)
        assert tracker.record_success(600.0) is not None
        assert tracker.record_success(900.0) is None
        assert tracker.record_success(1200.0) is None

    def test_healthy_process_never_reports_recovery(self, tracker):
        assert tracker.record_success(0.0) is None
        assert not tracker.active

    def test_second_outage_starts_clean(self, tracker):
        """第一次故障的轮数不能算进第二次。"""
        for i in range(5):
            tracker.record_failure(i * 300.0)
        tracker.record_success(1800.0)
        tracker.record_failure(7200.0)
        assert tracker.rounds == 1
        assert tracker.elapsed(7500.0) == 300.0


class TestDurationWording:
    """告警里「5 小时 5 分钟」比「18300 秒」有用——这条是给人读的。"""

    @pytest.mark.parametrize("seconds,expected", [
        (18300, "5 小时 5 分钟"),
        (3600, "1 小时 0 分钟"),
        (300, "5 分钟"),
        (42, "42 秒"),
        (0, "0 秒"),
    ])
    def test_format(self, seconds, expected):
        assert monitor._format_duration(seconds) == expected


# ── 第二组：接线层 ──────────────────────────────────────────────────


def _main_loop_handlers() -> dict[str, ast.ExceptHandler]:
    """main_loop 里 ``while True`` 那个 try 的各 except 分支，按异常类型名索引。

    只取该 try 的**直接** handler，不能 ``ast.walk``：分支体内还有嵌套的
    ``except Exception``（告警通道自己的兜底），walk 会让内层覆盖外层，
    于是「外层没接线」这个正是要抓的 bug 反而被内层遮住。
    """
    tree = ast.parse(MONITOR_PY.read_text())
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "main_loop"
    )
    loop = next(n for n in ast.walk(fn) if isinstance(n, ast.While))
    try_stmt = next(n for n in loop.body if isinstance(n, ast.Try))
    handlers: dict[str, ast.ExceptHandler] = {}
    for node in try_stmt.handlers:
        t = node.type
        name = (
            t.id if isinstance(t, ast.Name)
            else getattr(t, "attr", "") if isinstance(t, ast.Attribute)
            else "*"
        )
        handlers[name] = node
    return handlers


def _calls_in(node: ast.AST) -> set[str]:
    """节点内出现的调用名（含 ``a.b()`` 的 ``a.b``）。"""
    out: set[str] = set()
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if isinstance(f, ast.Name):
            out.add(f.id)
        elif isinstance(f, ast.Attribute):
            base = f.value.id if isinstance(f.value, ast.Name) else ""
            out.add(f"{base}.{f.attr}" if base else f.attr)
    return out


#: 这些 except 分支意味着「这一轮所有 source 都没成」。run_once 上抛之后
#: watchdog 不会执行，只有它们自己有机会说话。
TOTAL_FAILURE_HANDLERS = ["BlockedError", "ScrapeNetworkError", "ProxyError", "Exception"]


class TestEveryTotalFailureBranchAlerts:
    @pytest.mark.parametrize("exc", TOTAL_FAILURE_HANDLERS)
    def test_branch_records_the_outage(self, exc):
        handlers = _main_loop_handlers()
        assert exc in handlers, f"main_loop 不再捕获 {exc}——请同步本测试"
        assert "_outage.record_failure" in _calls_in(handlers[exc]), (
            f"except {exc} 分支没有调 _outage.record_failure()，"
            "全面故障时 admin 收不到任何通知（2026-08-05 就是这么静默了 5 小时）"
        )

    @pytest.mark.parametrize("exc", TOTAL_FAILURE_HANDLERS)
    def test_branch_actually_sends(self, exc):
        """光记账不发通知等于没修。"""
        assert "_alert_outage" in _calls_in(_main_loop_handlers()[exc])

    def test_success_path_clears_and_reports(self):
        src = inspect.getsource(monitor.main_loop)
        assert "_outage.record_success" in src, "成功后不清零，故障态会一直挂着"
        assert "_alert_outage_recovered" in src, "恢复了不通知，收到告警的人只能一直等"

    def test_recovery_is_checked_right_after_run_once(self):
        """必须紧跟 run_once：之后的剪枝 / watchdog 自己也会抛，那会被
        ``except Exception`` 记成新一轮「全面故障」，可抓取明明成功了。"""
        src = inspect.getsource(monitor.main_loop)
        after = src.index("record_success")
        assert after < src.index("prune_round_stats")
        assert after < src.index("_dispatch_watchdog_alerts")


class TestOutageAlertsAreAdminOnly:
    """故障告警只发 admin。用户对代理欠费、WAF 屏蔽无从处置。"""

    @pytest.mark.parametrize("fn", ["_alert_outage", "_alert_outage_recovered"])
    def test_goes_through_admin_helper(self, fn):
        src = inspect.getsource(getattr(monitor, fn))
        assert "_notify_admin_only" in src

    def test_recovery_uses_a_distinct_kind(self):
        """push 的 dedup 键是 (admin, kind)；同 kind 会把「已恢复」压掉。"""
        fired = inspect.getsource(monitor._alert_outage)
        healed = inspect.getsource(monitor._alert_outage_recovered)
        assert 'kind="outage"' in fired
        assert 'kind="outage_recovered"' in healed


class TestAlertActuallyComesOut:
    """把 main_loop 真跑起来：AST 只能证明「接线了」，证明不了「响了」。

    昨晚那个 bug 正是「日志写了、通知没发」——只有端到端才分得清这两件事。
    """

    @pytest.fixture
    def harness(self, monkeypatch):
        import asyncio
        import types

        from scrapers.base import ScrapeNetworkError

        sent: list[tuple[str, str]] = []

        class _FakeWebNotifier:
            async def send_error(self, message: str) -> bool:
                sent.append(("web", message))
                return True

        async def _fake_dispatch_admin(storage, message, *, kind="blocked"):
            sent.append((f"push:{kind}", message))
            return 1

        from mcore import push as _push
        monkeypatch.setattr(_push, "dispatch_admin", _fake_dispatch_admin)

        #: 继承 BaseException——``except Exception`` 分支不能把跳出信号吃掉，
        #: 否则测试收不了尾，还会顺带多发一条「未预期异常」告警。
        class _Stop(BaseException):
            pass

        async def _fake_sleep(seconds):
            """冷却不真睡。时钟不动，退避窗口一次都不到期。"""

        monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
        monkeypatch.setattr(monitor, "get_interval", lambda cfg: (300, False))
        # 成功那一轮会走到轮末的等待循环，它按真实时钟等 effective_interval。
        # 抖动函数归零 → deadline 立刻到期 → 直接进下一轮，测试不用真等 5 分钟。
        monkeypatch.setattr(monitor, "apply_jitter", lambda value, ratio: 0.0)

        cfg = types.SimpleNamespace(
            check_interval=300, min_interval=60, peak_interval=120,
            peak_start="08:00", peak_end="10:00",
            peak_start_2="18:00", peak_end_2="20:00",
            cities=[], jitter_ratio=0.0,
            heartbeat_interval_minutes=0,  # 关掉心跳，它不是本文件要测的东西
        )

        def _drive(script):
            """按 script 逐轮决定 run_once 的结果，跑完即跳出。

            ``"fail"`` = 所有 source 网络不可达（2026-08-05 的形态），
            ``"ok"``   = 正常一轮。
            """
            monkeypatch.setattr(monitor, "_outage", monitor._OutageTracker())
            sent.clear()
            remaining = list(script)

            async def _scripted(*a, **kw):
                if not remaining:
                    raise _Stop
                if remaining.pop(0) == "fail":
                    raise ScrapeNetworkError(
                        "Holland2Stay 主站加载失败（流量配额耗尽或账户欠费）: "
                        "net::ERR_TUNNEL_CONNECTION_FAILED"
                    )
                return {}

            monkeypatch.setattr(monitor, "run_once", _scripted)
            with pytest.raises(_Stop):
                asyncio.run(monitor.main_loop(cfg, storage, [], _FakeWebNotifier()))
            return list(sent)

        # 成功那一轮会走到收尾步骤；这里只需要它们别报错。
        storage = types.SimpleNamespace(
            set_meta=lambda *a, **kw: None,
            record_uptime_sample=lambda *a, **kw: None,
            prune_round_stats=lambda *a, **kw: None,
        )
        return types.SimpleNamespace(drive=_drive)

    def test_total_outage_reaches_admin(self, harness):
        sent = harness.drive(["fail"] * 40)
        assert sent, "所有 source 连续失败，admin 一条告警都没收到"

        first = sent[0][1]
        assert "全面抓取故障" in first
        # 代理探测的结论要原样透出去，省掉一轮上服务器翻日志
        assert "流量配额耗尽或账户欠费" in first

    def test_admin_is_not_flooded(self, harness):
        """40 轮连续失败只该发第一条——退避窗口一次都没到期。"""
        pushes = [m for chan, m in harness.drive(["fail"] * 40) if chan.startswith("push:")]
        assert len(pushes) == 1, f"发了 {len(pushes)} 条"

    def test_alert_is_admin_only(self, harness):
        """用户渠道一条都不能有——代理欠费不是用户能处置的事。"""
        for chan, _ in harness.drive(["fail"] * 40):
            assert chan == "web" or chan.startswith("push:"), chan

    def test_recovery_is_announced(self, harness):
        """收到「挂了」的人得能收到「好了」，否则只能自己上服务器确认。"""
        kinds = [c for c, _ in harness.drive(["fail"] * 5 + ["ok"])]
        assert "push:outage" in kinds
        assert "push:outage_recovered" in kinds, "故障恢复了没通知"

    def test_no_recovery_notice_without_an_outage(self, harness):
        """一路正常时不能冒出「已恢复」——那会让人以为自己错过了一次故障。"""
        assert harness.drive(["ok"] * 5) == []

    def test_a_blip_below_threshold_stays_quiet(self, harness):
        """两轮网络抖动自愈，不该惊动任何人（阈值 3）。"""
        assert harness.drive(["fail", "fail", "ok"]) == []


class TestWatchdogSkipRemainsJustified:
    """watchdog 在 run_once 上抛时不执行，是因为 _OutageTracker 顶上了。

    这个前提一旦被删，盲区立刻复现——而且是静默复现。
    """

    def test_docstring_names_the_replacement(self):
        doc = monitor._dispatch_watchdog_alerts.__doc__ or ""
        assert "_OutageTracker" in doc or "_alert_outage" in doc
