"""数据健康判级 + 退化告警测试。

判级规则本身才是最容易写错的部分，所以主要用
``source_health_from_rows()`` 这个纯函数直接喂行、直接看级别，
不经过 DB。

最关键的一条契约：**「抓到 0 条」不等于「坏了」**。
Xior 四栋楼常态零可订，OurCampus 官网自述排队 16–18 个月。把它们钉在告警上
会让真信号被噪音淹掉，而告警一旦被无视，等于没有告警。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mcore import health, watchdog
from models import Listing
from storage import Storage


#: _rows 生成的最新一轮的时刻。判级现在带时间维度（「多久没有完整扫描」），
#: 纯函数测试必须把 now 钉死，否则用例会随真实时间漂移。
_NOW = datetime(2026, 8, 3, 20, 0, 0, tzinfo=timezone.utc)


def _rows(specs):
    """specs 最新在前，每项 (listings, targets, complete, error_type)。

    最新一轮落在 ``_NOW``，往回每轮相隔 1 分钟——真实轮次就是分钟级的，
    用小时级会让 24 轮的窗口横跨一整天，与生产完全不像。
    """
    return [
        {
            "round_at": (_NOW - timedelta(minutes=i)).isoformat(),
            "source": "s",
            "listings": l, "targets": t, "complete": c,
            "duration_ms": 0, "error_type": e, "error_msg": "",
        }
        for i, (l, t, c, e) in enumerate(specs)
    ]


def _h(rows, *, last_complete_at=None, now=_NOW):
    """判级快捷方式：默认从 rows 里取最近一条 complete > 0 当作上次完整扫描。

    生产里这个值是**跨窗口**从库里查的（见 source_health_from_rows 的
    docstring）。测试默认从 rows 推，是因为绝大多数用例的窗口本就覆盖了它；
    要验证「窗口内没有完整轮」的场景，显式传 last_complete_at。
    """
    if last_complete_at is None:
        last_complete_at = next(
            (r["round_at"] for r in rows
             if int(r["complete"]) > 0 and not r["error_type"]),
            "",
        )
    return health.source_health_from_rows(
        "s", rows, last_complete_at=last_complete_at, now=now,
    )


def _ok(n, listings=10):
    return [(listings, 2, 2, "")] * n


def _zero(n):
    return [(0, 2, 2, "")] * n


def _fail(n, err="RateLimitError"):
    return [(0, 2, 0, err)] * n


# ── 判级 ────────────────────────────────────────────────────────────


class TestJudgeStatus:
    def test_healthy_source_is_ok(self):
        h = _h(_rows(_ok(10)))
        assert h.status == health.STATUS_OK
        assert h.reasons == []

    def test_no_rows_is_unknown(self):
        h = _h([])
        assert h.status == health.STATUS_UNKNOWN
        assert h.rounds == 0

    def test_consecutive_failures_is_down(self):
        h = _h(_rows(_fail(3) + _ok(5)))
        assert h.status == health.STATUS_DOWN
        assert h.fail_streak == 3
        assert h.last_error == "RateLimitError"

    def test_below_fail_threshold_is_not_down(self):
        h = _h(_rows(_fail(2) + _ok(5)))
        assert h.status != health.STATUS_DOWN

    def test_zero_after_nonzero_is_warn(self):
        """本来有房、突然全没了——上游改版打坏解析器就是这个特征。"""
        h = _h(_rows(_zero(3) + _ok(5, listings=284)))
        assert h.status == health.STATUS_WARN
        assert h.zero_streak == 3
        assert h.max_listings == 284

    def test_always_zero_source_stays_ok(self):
        """Xior / OurCampus 常态零可订，不该被永久钉在告警上。"""
        h = _h(_rows(_zero(20)))
        assert h.status == health.STATUS_OK
        assert h.zero_streak == 20
        assert h.max_listings == 0

    def test_low_completeness_alone_is_not_warn(self):
        """低完整率**不再**告警——分层抓取让它对 H2S 永久为真。

        H2S 每轮只查 _FRESH_STATUSES（一律 complete=False），只有每 30 分钟
        一次的全量轮才可能完整，完整率结构性地停在 10% 上下。按 80% 阈值报，
        它每天都在响，唯一的效果是训练人忽略告警。

        指标本身保留在面板上，只是不再作为判据。
        """
        rows = _rows([(10, 6, 2, "")] * 5)   # 2/6 完整，且每轮都有完整 target
        h = _h(rows)
        assert h.completeness_rate == pytest.approx(2 / 6)
        assert h.status == health.STATUS_OK
        assert h.reasons == []

    def test_full_completeness_is_ok(self):
        h = _h(_rows(_ok(5)))
        assert h.completeness_rate == pytest.approx(1.0)
        assert h.status == health.STATUS_OK


class TestStreaks:
    def test_fail_streak_breaks_on_success(self):
        h = _h(_rows(_fail(2) + _ok(1) + _fail(5)))
        assert h.fail_streak == 2

    def test_failed_round_breaks_zero_streak(self):
        """失败轮的 listings 恒为 0；若并入 zero_streak，任何一次失败都会顺带
        触发「零房源」告警，两条规则就重了。"""
        h = _h(_rows(_fail(1) + _zero(5) + _ok(3)))
        assert h.zero_streak == 0
        assert h.fail_streak == 1

    def test_last_success_and_nonzero_timestamps(self):
        rows = _rows(_fail(2) + _zero(1) + _ok(1, listings=7))
        h = _h(rows)
        assert h.last_success_at == rows[2]["round_at"]   # 第一条非 error
        assert h.last_nonzero_at == rows[3]["round_at"]   # 第一条 listings>0

    def test_averages_exclude_failed_rounds(self):
        """失败轮的 0 不该把平均值拉下来——那是「没抓」，不是「抓到 0 条」。"""
        h = _h(_rows(_fail(5) + _ok(2, listings=10)))
        assert h.avg_listings == pytest.approx(10.0)


class TestFmtTs:
    """告警文案里的时间必须是本地时间。

    库里存 UTC，但容器跑在 TZ=Europe/Amsterdam，日志的 asctime 就是那个时区。
    告警里塞 UTC ISO 的话，收到推送的人还得自己换算才能去日志里对上那一刻。
    """

    def test_converts_utc_to_configured_zone(self, monkeypatch):
        monkeypatch.setenv("TIMEZONE", "Europe/Amsterdam")
        assert health.fmt_ts("2026-08-03T12:00:00+00:00") == "08-03 14:00"   # CEST +2

    def test_handles_dst_boundary(self, monkeypatch):
        monkeypatch.setenv("TIMEZONE", "Europe/Amsterdam")
        assert health.fmt_ts("2026-01-15T12:00:00+00:00") == "01-15 13:00"   # CET +1

    def test_respects_other_zones(self, monkeypatch):
        monkeypatch.setenv("TIMEZONE", "UTC")
        assert health.fmt_ts("2026-08-03T12:00:00+00:00") == "08-03 12:00"

    def test_naive_timestamp_is_treated_as_utc(self, monkeypatch):
        monkeypatch.setenv("TIMEZONE", "Europe/Amsterdam")
        assert health.fmt_ts("2026-08-03T12:00:00") == "08-03 14:00"

    def test_empty_uses_fallback(self):
        assert health.fmt_ts("", fallback="窗口内没有") == "窗口内没有"
        assert health.fmt_ts("") == ""

    def test_garbage_is_returned_as_is(self):
        """展示层，不该因为一个脏时间戳把告警发不出去。"""
        assert health.fmt_ts("not-a-time") == "not-a-time"

    def test_invalid_zone_does_not_raise(self, monkeypatch):
        monkeypatch.setenv("TIMEZONE", "Mars/Olympus_Mons")
        assert health.fmt_ts("2026-08-03T12:00:00+00:00")  # 不炸即可


class TestAlertTimestampsAreLocal:
    def test_down_alert_body_has_no_raw_iso(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TIMEZONE", "Europe/Amsterdam")
        st = Storage(Path(tmp_path) / "t.db", timezone_str="UTC")
        try:
            for i, (l, e) in enumerate([(12, "")] * 3 + [(0, "RateLimitError")] * 3):
                st.record_round_stat(
                    round_at=f"2026-08-03T{i:02d}:00:00+00:00", source="s",
                    listings=l, targets=2, complete=0 if e else 2, error_type=e,
                )
            body = watchdog.poll(st)[0].body
            assert "+00:00" not in body, f"告警文案漏了 UTC 原文: {body}"
            assert "02-00" not in body
            assert "04:00" in body, f"应显示为本地时间 04:00（UTC 02:00）: {body}"
        finally:
            st.close()


class TestOverallStatus:
    def test_worst_wins(self):
        def mk(status):
            h = health.SourceHealth(source="x")
            h.status = status
            return h
        assert health.overall_status([mk("ok"), mk("warn")]) == health.STATUS_WARN
        assert health.overall_status([mk("ok"), mk("warn"), mk("down")]) == health.STATUS_DOWN
        assert health.overall_status([mk("ok"), mk("ok")]) == health.STATUS_OK
        assert health.overall_status([]) == health.STATUS_UNKNOWN


# ── DB 集成 ─────────────────────────────────────────────────────────


@pytest.fixture
def st(tmp_path):
    s = Storage(Path(tmp_path) / "t.db", timezone_str="UTC")
    yield s
    s.close()


#: 走 DB 的用例共用的时间锚。**必须在模块加载时算一次**，不能每次调用现取:
#: silent_round_streak 按 round_at 把各 source 归成同一轮，两次 _seed 若各自
#: 取 now()，毫秒差会把一轮劈成两轮。
_DB_NOW = datetime.now(timezone.utc)


def _seed(st, source, specs):
    """specs 最旧在前，每项 (listings, targets, complete, error_type)。

    时刻取「相对现在往回数分钟」而非固定日期：stale 规则量的是与当下的真实
    时间差，钉死日期会让每一轮都显得陈旧数年，所有走 DB 的用例集体误报。
    """
    n = len(specs)
    for i, (l, t, c, e) in enumerate(specs):
        st.record_round_stat(
            round_at=(_DB_NOW - timedelta(minutes=n - 1 - i)).isoformat(),
            source=source,
            listings=l, targets=t, complete=c, error_type=e,
        )


class TestSilentRoundStreak:
    def test_counts_consecutive_global_zero_rounds(self, st):
        _seed(st, "a", [(5, 1, 1, "")] * 3 + [(0, 1, 1, "")] * 4)
        _seed(st, "b", [(5, 1, 1, "")] * 3 + [(0, 1, 1, "")] * 4)
        assert health.silent_round_streak(st) == 4

    def test_zero_when_latest_round_has_data(self, st):
        _seed(st, "a", [(0, 1, 1, "")] * 5 + [(3, 1, 1, "")])
        assert health.silent_round_streak(st) == 0

    def test_any_source_with_data_breaks_streak(self, st):
        _seed(st, "a", [(0, 1, 1, "")] * 5)
        _seed(st, "b", [(0, 1, 1, "")] * 4 + [(1, 1, 1, "")])
        assert health.silent_round_streak(st) == 0


class TestHealthReport:
    def test_report_shape(self, st):
        _seed(st, "xior", [(0, 4, 4, "")] * 5)
        rep = health.health_report(st)
        assert rep["status"] == health.STATUS_OK
        assert [s["source"] for s in rep["sources"]] == ["xior"]
        assert "fail_streak_down" in rep["thresholds"]


# ── watchdog ────────────────────────────────────────────────────────


class TestWatchdog:
    def test_fires_for_down_source(self, st):
        _seed(st, "ourdomain", [(12, 2, 2, "")] * 5 + [(0, 2, 0, "RateLimitError")] * 3)
        alerts = watchdog.poll(st)
        assert [a.key for a in alerts] == ["source_down:ourdomain"]
        assert alerts[0].level == watchdog.LEVEL_DOWN

    def test_fires_for_zero_after_nonzero(self, st):
        _seed(st, "holland2stay", [(284, 6, 6, "")] * 5 + [(0, 6, 6, "")] * 3)
        assert [a.key for a in watchdog.poll(st)] == ["source_zero:holland2stay"]

    def test_silent_source_never_fires(self, st):
        _seed(st, "xior", [(0, 4, 4, "")] * 20)
        assert watchdog.poll(st) == []

    def test_down_suppresses_that_sources_warns(self, st):
        """down 和 warn 说的是同一件事，不该同时报两条。"""
        _seed(st, "s", [(50, 6, 6, "")] * 5 + [(0, 6, 0, "BlockedError")] * 4)
        keys = [a.key for a in watchdog.poll(st)]
        assert keys == ["source_down:s"]

    def test_throttled_on_second_poll(self, st):
        _seed(st, "s", [(12, 2, 2, "")] * 5 + [(0, 2, 0, "RateLimitError")] * 3)
        assert watchdog.poll(st, now=1000.0)
        assert watchdog.poll(st, now=1100.0) == []

    def test_refires_after_repeat_interval(self, st):
        _seed(st, "s", [(12, 2, 2, "")] * 5 + [(0, 2, 0, "RateLimitError")] * 3)
        assert watchdog.poll(st, now=1000.0)
        later = 1000.0 + watchdog._REPEAT_INTERVAL + 1
        assert [a.key for a in watchdog.poll(st, now=later)] == ["source_down:s"]

    def test_recovery_is_reported_once(self, st):
        _seed(st, "s", [(12, 2, 2, "")] * 3 + [(0, 2, 0, "RateLimitError")] * 3)
        assert [a.key for a in watchdog.poll(st, now=1000.0)] == ["source_down:s"]
        # 恢复：追加三轮成功。时刻必须**晚于** _seed 那批，否则按 round_at
        # 倒序取窗口时它们排在后面，恢复根本不会被看到。
        for i in range(1, 4):
            st.record_round_stat(
                round_at=(_DB_NOW + timedelta(minutes=i)).isoformat(),
                source="s", listings=12, targets=2, complete=2)
        recovered = watchdog.poll(st, now=1100.0)
        assert [a.level for a in recovered] == [watchdog.LEVEL_RECOVERED]
        assert watchdog.poll(st, now=1200.0) == []

    def test_throttle_survives_restart(self, st, tmp_path):
        """节流状态存 meta 而不是内存：supervisor 的 autorestart 恰恰会在故障时
        频繁重启，节流放内存等于最该节流的时候失效。"""
        db = Path(tmp_path) / "t.db"
        _seed(st, "s", [(12, 2, 2, "")] * 3 + [(0, 2, 0, "RateLimitError")] * 3)
        # st 已经是这个库；先发一次
        assert watchdog.poll(st, now=1000.0)
        st.close()
        # 「重启」= 新建一个 Storage 实例连同一个库
        st2 = Storage(db, timezone_str="UTC")
        try:
            assert watchdog.poll(st2, now=1050.0) == []
        finally:
            st2.close()

    def test_poll_never_raises(self, st):
        st._conn.close()
        assert watchdog.poll(st) == []
        assert watchdog.snapshot(st) == []

    def test_silent_rounds_fires_when_db_has_listings(self, st):
        """2026-06-13 那次 7 周静默停摆的直接判据：库里有房源，但一轮都抓不到。"""
        st.diff([Listing(
            id="x1", name="X", status="Available to book", price_raw="€700",
            available_from="2030-01-01", features=[], url="https://e.test/1",
            city="C", source="a",
        )])
        _seed(st, "a", [(0, 1, 1, "")] * 8)
        assert "silent_rounds" in [a.key for a in watchdog.poll(st)]

    def test_silent_rounds_silent_on_empty_db(self, st):
        """空库分不清「坏了」和「本来就没房」，不报。"""
        _seed(st, "a", [(0, 1, 1, "")] * 8)
        assert "silent_rounds" not in [a.key for a in watchdog.poll(st)]

    def test_silent_rounds_survives_a_long_outage(self, st):
        """基线取 listings 表而不是判定窗口：窗口只有二十来轮（约 2 小时），
        若拿窗口做基线，故障超过 2 小时后告警会自己闭嘴。"""
        st.diff([Listing(
            id="x1", name="X", status="Available to book", price_raw="€700",
            available_from="2030-01-01", features=[], url="https://e.test/1",
            city="C", source="a",
        )])
        # 窗口内**全部**是零轮，没有任何非零轮可作基线
        _seed(st, "a", [(0, 1, 1, "")] * 24)
        assert "silent_rounds" in [a.key for a in watchdog.poll(st)]

    def test_snapshot_does_not_advance_state(self, st):
        _seed(st, "s", [(12, 2, 2, "")] * 5 + [(0, 2, 0, "RateLimitError")] * 3)
        assert len(watchdog.snapshot(st)) == 1
        assert len(watchdog.snapshot(st)) == 1
        # snapshot 不写节流，poll 仍然能发出来
        assert watchdog.poll(st, now=1000.0)


# ── 分轮抓取 ────────────────────────────────────────────────────────


def _row(listings, targets, total, err=""):
    return {
        "round_at": _NOW.isoformat(), "source": "s",
        "listings": listings, "targets": targets, "complete": targets,
        "duration_ms": 0, "error_type": err, "error_msg": "",
        "total_targets": total,
    }


class TestShardedSourcesSkipZeroRule:
    """分轮抓取会打破 zero_streak 规则的前提。

    分片后每轮抓的是不同的 target 子集，某一轮 0 条只说明「这一片的楼没房」，
    和上一轮的非零根本不是同一批楼。2026-08-03 Xior 扩到 30 栋分片后立刻误报。
    """

    def test_sharded_zero_streak_does_not_warn(self):
        rows = [_row(0, 3, 30)] * 3 + [_row(38, 3, 30)]
        h = health.source_health_from_rows(
            "xior", rows, last_complete_at=_NOW.isoformat(), now=_NOW)
        assert h.sharded is True
        assert h.zero_streak == 3          # 仍然如实统计
        assert h.status == health.STATUS_OK  # 但不据此告警

    def test_unsharded_zero_streak_still_warns(self):
        rows = [_row(0, 6, 6)] * 3 + [_row(284, 6, 6)]
        h = health.source_health_from_rows(
            "holland2stay", rows, last_complete_at=_NOW.isoformat(), now=_NOW)
        assert h.sharded is False
        assert h.status == health.STATUS_WARN

    def test_missing_total_targets_treated_as_unsharded(self):
        """老行没有这一列，保守按不分片处理，规则照常生效。"""
        rows = [_row(0, 6, 0)] * 3 + [_row(284, 6, 0)]
        h = health.source_health_from_rows(
            "holland2stay", rows, last_complete_at=_NOW.isoformat(), now=_NOW)
        assert h.sharded is False
        assert h.status == health.STATUS_WARN

    def test_sharded_still_reports_fail_streak(self):
        """分片只跳过零房源规则，连续失败照常判 down。"""
        rows = [_row(0, 3, 30, "RateLimitError")] * 3 + [_row(38, 3, 30)]
        h = health.source_health_from_rows(
            "xior", rows, last_complete_at=_NOW.isoformat(), now=_NOW)
        assert h.status == health.STATUS_DOWN

    def test_sharded_flag_is_exposed(self):
        h = health.source_health_from_rows("xior", [_row(0, 3, 30)])
        assert h.as_dict()["sharded"] is True


class TestAlertTextIsTerse:
    """告警文案只陈述发生了什么，不写解读。"""

    def test_no_interpretive_prose(self, st):
        _seed(st, "s", [(12, 2, 2, "")] * 5 + [(0, 2, 0, "RateLimitError")] * 3)
        body = watchdog.poll(st)[0].body
        for banned in ("就是这个样子", "请求仍是 200", "会一直挂着", "只是不产出数据"):
            assert banned not in body, f"告警文案不该含解读性文字: {body}"
        assert "\n" not in body, "单行，便于推送展示"


# ── 分层抓取 / 熔断：两条不该触发告警的「正常」 ──────────────────


class TestTieredScrapingDoesNotAlert:
    """分层抓取让完整率结构性偏低，那不是故障。

    H2S 每轮只查 _FRESH_STATUSES，一律 complete=False；只有每 30 分钟一次的
    全量轮才可能为 True。生产实测完整率长期在 9–11%，而阈值是 80%——这条告警
    每天都在响。告警一旦被无视，等于没有告警。
    """

    def _tiered(self, *, full_scan_ago_minutes, rounds=24):
        """模拟 H2S：绝大多数轮 complete=0，全量轮在 N 分钟前。"""
        rows = _rows([(46, 2, 0, "")] * rounds)
        return health.source_health_from_rows(
            "holland2stay", rows,
            last_complete_at=(_NOW - timedelta(minutes=full_scan_ago_minutes)).isoformat(),
            now=_NOW,
        )

    def test_tiered_source_on_schedule_is_ok(self):
        """全量按 30 分钟节奏跑着，完整率 0% 也不该告警。"""
        h = self._tiered(full_scan_ago_minutes=31)
        assert h.completeness_rate == 0.0
        assert h.status == health.STATUS_OK
        assert h.reasons == []

    def test_tiered_source_alerts_only_when_full_scan_stops(self):
        """真正的故障是全量停了——那时 stale 收敛不再执行，下架判定滞后。"""
        h = self._tiered(full_scan_ago_minutes=95)
        assert h.status == health.STATUS_WARN
        assert any("没有完整扫描" in r for r in h.reasons)

    def test_threshold_boundary(self):
        """阈值是 90 分钟（5400 秒），刚好卡住不报，多一分钟才报。"""
        assert health.STALE_FULL_SCAN_SECONDS == 5400
        assert self._tiered(full_scan_ago_minutes=90).status == health.STATUS_OK
        assert self._tiered(full_scan_ago_minutes=91).status == health.STATUS_WARN

    def test_never_complete_uses_window_as_lower_bound(self):
        """从未有过完整轮时答「至少这么久」，而不是当作 0。"""
        rows = _rows([(46, 2, 0, "")] * 24)
        h = health.source_health_from_rows(
            "holland2stay", rows, last_complete_at="", now=_NOW + timedelta(hours=3))
        assert h.status == health.STATUS_WARN
        assert any("没有完整扫描" in r for r in h.reasons)

    def test_stale_is_measured_against_now_not_last_round(self):
        """进程卡死不再写遥测时，最该报警——不能用最后一行当基准把差值冻住。"""
        rows = _rows(_ok(5))          # 全部完整，但都是 3 小时前的
        h = health.source_health_from_rows(
            "s", rows,
            last_complete_at=_NOW.isoformat(),
            now=_NOW + timedelta(hours=3),
        )
        assert h.stale_full_scan_seconds == pytest.approx(3 * 3600)
        assert h.status == health.STATUS_WARN


class TestCircuitOpenIsNotAFailure:
    """熔断跳过的轮次是**按设计退避**，不是抓取失败。

    原先把它当失败计入 fail_streak，后果是熔断器每正常工作一次就发一对
    down + recovered 告警：Xior 首次失败即跳闸，「1 次真失败 + 2 轮熔断」凑够
    3 轮，报出「⛔ 连续抓取失败｜错误 CircuitOpen」。保护装置一生效就拉警报。
    """

    def _circuit(self, n):
        return [(0, 4, 0, health.CIRCUIT_OPEN_ERROR)] * n

    def test_circuit_open_rounds_do_not_make_a_source_down(self):
        """生产实况：1 次真 429 之后连续熔断，不该报 down。"""
        h = _h(_rows(self._circuit(8) + _fail(1) + _ok(10)))
        assert h.fail_streak == 1
        assert h.circuit_open_rounds == 8
        assert h.status == health.STATUS_OK

    def test_pure_circuit_open_window_is_not_down(self):
        h = _h(_rows(self._circuit(24)), last_complete_at=_NOW.isoformat())
        assert h.fail_streak == 0
        assert h.status == health.STATUS_OK

    def test_real_failures_still_accumulate_across_circuit_gaps(self):
        """跳过而不是打断：canary 反复失败仍然是真的 down。"""
        rows = _rows(
            _fail(1) + self._circuit(3) + _fail(1) + self._circuit(3) + _fail(1) + _ok(5)
        )
        h = _h(rows)
        assert h.fail_streak == 3
        assert h.status == health.STATUS_DOWN

    def test_real_failure_after_circuit_still_breaks_on_success(self):
        """熔断轮中立不等于「无视成功」：成功仍然清零。"""
        h = _h(_rows(self._circuit(2) + _ok(1) + _fail(5)))
        assert h.fail_streak == 0

    def test_last_error_points_at_the_real_cause(self):
        """down 告警的正文要说 403/429，而不是 CircuitOpen。"""
        h = _h(_rows(self._circuit(3) + _fail(3, "BlockedError") + _ok(5)))
        assert h.fail_streak == 3
        assert h.last_error == "BlockedError"
        assert h.status == health.STATUS_DOWN

    def test_circuit_open_does_not_break_zero_streak(self):
        """熔断轮 listings 恒为 0，但它既不算失败也不该打断零计数。"""
        h = _h(_rows(self._circuit(2) + _zero(3) + _ok(3, listings=284)))
        assert h.zero_streak == 3


class TestWatchdogAlertShape:
    def test_stale_full_scan_alert_replaces_completeness_low(self, st):
        """告警键换了名字，正文要说清后果（stale 收敛不执行）。"""
        # 一轮都没完整过，且窗口跨度已经超过 90 分钟
        _seed(st, "s", [(46, 2, 0, "")] * 120)
        for a in watchdog.evaluate(st, window=120):
            if a.key.startswith("stale_full_scan:"):
                assert "没有完整轮" in a.body
                assert "stale 收敛" in a.body
                break
        else:
            pytest.fail("从未有过完整轮，应当报 stale_full_scan")

    def test_never_complete_is_conservative_inside_a_short_window(self, st):
        """从未完整过时，下界只能取窗口最早那轮——**刻意宁可漏报不误报**。

        默认窗口 24 轮，按生产的分钟级节奏只覆盖半小时左右，够不到 90 分钟的
        阈值。这不是漏洞：真实的「全量停了」场景里 last_complete_at 来自库里、
        不受窗口限制，照样算得出（见 last_complete_round_at 的 docstring）。
        真正的空值只出现在「保留期内一次都没完整过」，那种 source 由
        fail_streak 与全局静默规则兜底。
        """
        _seed(st, "s", [(46, 2, 0, "")] * 24)
        assert not any(
            a.key.startswith("stale_full_scan:") for a in watchdog.evaluate(st)
        )

    def test_no_completeness_low_key_remains(self, st):
        """旧告警键必须彻底消失，否则节流状态会指向一条不再产生的告警。"""
        _seed(st, "s", [(10, 6, 2, "")] * 6)
        assert not any(
            a.key.startswith("completeness_low:") for a in watchdog.evaluate(st)
        )

    def test_circuit_open_source_produces_no_down_alert(self, st):
        _seed(st, "xior",
              [(0, 4, 4, "")] * 3
              + [(0, 4, 0, "RateLimitError")]
              + [(0, 4, 0, health.CIRCUIT_OPEN_ERROR)] * 4)
        assert not any(
            a.key.startswith("source_down:") for a in watchdog.evaluate(st)
        )


class TestLastCompleteRoundAt:
    """跨窗口查「上次完整扫描」——H2S 的窗口比它的全量间隔还短。"""

    def test_finds_the_most_recent_complete_round(self, st):
        _seed(st, "s", [(46, 2, 2, "")] + [(46, 2, 0, "")] * 5)
        rows = st.recent_round_stats(source="s", limit=6)
        got = st.last_complete_round_at("s")
        assert got == rows[-1]["round_at"]          # 最早那轮才是唯一完整的

    def test_takes_the_newest_when_several_are_complete(self, st):
        """必须取**最新**的完整轮，不是最早的。

        取最早的话，一个跑了几天的 source 会永远报「上次完整扫描在几天前」
        ——正是这次要消灭的那种永久误报。单条完整轮的用例区分不出这两种实现。
        """
        _seed(st, "s", [
            (46, 2, 2, ""),      # 最早：完整
            (46, 2, 0, ""),
            (46, 2, 2, ""),      # 中间：完整
            (46, 2, 0, ""),
            (46, 2, 2, ""),      # 最新的完整轮 ← 应当取它
            (46, 2, 0, ""),
        ])
        rows = st.recent_round_stats(source="s", limit=6)   # 最新在前
        assert st.last_complete_round_at("s") == rows[1]["round_at"]
        assert st.last_complete_round_at("s") != rows[-1]["round_at"]

    def test_returns_empty_when_never_complete(self, st):
        _seed(st, "s", [(46, 2, 0, "")] * 5)
        assert st.last_complete_round_at("s") == ""

    def test_is_scoped_to_the_source(self, st):
        _seed(st, "a", [(46, 2, 2, "")] * 3)
        _seed(st, "b", [(46, 2, 0, "")] * 3)
        assert st.last_complete_round_at("a") != ""
        assert st.last_complete_round_at("b") == ""

    def test_reaches_past_the_health_window(self, st):
        """本方法存在的全部理由：默认窗口 24 轮盖不住 30 分钟的全量间隔。"""
        _seed(st, "s", [(46, 2, 2, "")] + [(46, 2, 0, "")] * 40)
        assert len(st.recent_round_stats(source="s", limit=health.DEFAULT_WINDOW)) == 24
        assert st.last_complete_round_at("s") != ""   # 窗口外也能找到


class TestCircuitMarkerIsOneString:
    """写入方与识别方必须用同一个常量。

    monitor 写遥测行、health 判读遥测行，两边靠一个字符串对上。项目里已经栽过
    一次同类的跟头（run_once 与 _dispatch_watchdog_alerts 各自注释假设对方在
    负责，中间是空的，代价是 5 小时零告警）。**注释描述的是意图，不是事实**，
    跨函数的交接必须由测试固定。

    这里钉的是「不许再出现字面量」：改动前把 error_type 写成 "circuit_open"
    之类，所有行为测试照样全绿，而生产里熔断轮会重新被算成抓取失败。
    """

    def test_monitor_writes_the_constant_health_reads(self):
        import monitor
        assert monitor.CIRCUIT_OPEN_ERROR is health.CIRCUIT_OPEN_ERROR

    def test_no_literal_circuit_marker_in_monitor(self):
        import ast
        import inspect
        import monitor

        src = inspect.getsource(monitor)
        tree = ast.parse(src)
        bad = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.keyword)
            and node.arg == "error_type"
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            and "circuit" in node.value.value.lower()
        ]
        assert not bad, (
            f"monitor.py:{bad} 用字面量写熔断标记。必须用 "
            f"mcore.health.CIRCUIT_OPEN_ERROR——两边一旦写岔，熔断轮会重新被"
            f"算成抓取失败，熔断器每正常工作一次就发一对 down + recovered 告警"
        )


# ── 分层抓取：高频轮的 0 不参与零房源判定 ──────────────────────────


class TestTieredZeroRoundsDoNotFeedZeroStreak:
    """高频轮的 0 和全量轮的非零不可比，不能放进同一个 streak。

    H2S 每轮只查「可订 / 抽签 / 即将上线」（一律 complete=0），每 30 分钟才做
    一次带 Reserved 的全量轮。2026-08-24 生产实测：平台上确实一套可订的都没有
    （43 套全是 Reserved），于是高频轮连续返回 0、全量轮返回 43，凑够 3 轮报
    「抓取成功但零房源」，下一次全量轮一到又「已恢复」——24 小时 7 报 7 恢复。

    这和 TestShardedSourcesSkipZeroRule 是同一个坑，只是切的维度是「状态」而非
    「楼栋」，而 sharded 的判据（total_targets > targets）在分层时恒为假。
    """

    def test_partial_zero_rounds_do_not_count(self):
        """5 个高频零轮 + 1 个全量非零轮 = 生产上最常见的形态。"""
        rows = _rows([(0, 2, 0, "")] * 5 + [(43, 2, 2, "")] * 3)
        h = _h(rows)
        assert h.zero_streak == 0
        assert h.status == health.STATUS_OK
        assert h.reasons == []

    def test_full_zero_rounds_still_warn(self):
        """规则没被废掉：全量轮连着抓到 0，才是真的「本来有房、突然全没了」。"""
        h = _h(_rows([(0, 2, 2, "")] * 3 + [(43, 2, 2, "")] * 3))
        assert h.zero_streak == 3
        assert h.status == health.STATUS_WARN

    def test_partial_rounds_are_skipped_not_terminal(self):
        """高频轮夹在中间时跳过而不是打断——否则真故障永远凑不够 3 轮。"""
        rows = _rows(
            [(0, 2, 2, "")]        # 全量零
            + [(0, 2, 0, "")] * 4  # 高频零，跳过
            + [(0, 2, 2, "")]      # 全量零
            + [(0, 2, 0, "")] * 4  # 高频零，跳过
            + [(0, 2, 2, "")]      # 全量零
            + [(43, 2, 2, "")]
        )
        h = _h(rows)
        assert h.zero_streak == 3
        assert h.status == health.STATUS_WARN

    def test_nonzero_partial_round_resets_the_streak(self):
        """高频轮抓到了东西 = 这个 source 明摆着在工作，不论那轮完不完整。"""
        rows = _rows(
            [(0, 2, 2, "")] * 2    # 全量零
            + [(3, 2, 0, "")]      # 高频轮抓到 3 条 → 重新计数
            + [(0, 2, 2, "")] * 5
            + [(43, 2, 2, "")]
        )
        h = _h(rows)
        assert h.zero_streak == 2
        assert h.status == health.STATUS_OK

    def test_failed_round_still_breaks_before_partial_logic(self):
        """失败轮照旧打断，和分层与否无关。"""
        h = _h(_rows(_fail(1) + [(0, 2, 2, "")] * 5 + [(43, 2, 2, "")]))
        assert h.zero_streak == 0


# ── 停用的 source 不再巡检 ─────────────────────────────────────────


class TestDisabledSourceIsNotJudged:
    """停用之后遥测行还会在库里躺满 30 天保留期。

    2026-08-24 实测：OurDomain 在 08-21 从面板停用，之后三天报了 5 次
    「迟迟没有完整扫描」——每一次都属实，也每一次都无意义。
    """

    def test_disabled_source_is_unknown(self):
        rows = _rows(_ok(5))
        h = health.source_health_from_rows(
            "ourdomain", rows,
            last_complete_at=(_NOW - timedelta(hours=10)).isoformat(),
            now=_NOW, enabled=False,
        )
        assert h.status == health.STATUS_UNKNOWN
        assert h.reasons == ["该 source 当前未启用，窗口内的遥测是停用前留下的"]

    def test_same_rows_warn_when_enabled(self):
        """对照组：同样的行，启用状态下是要报的。"""
        rows = _rows(_ok(5))
        h = health.source_health_from_rows(
            "ourdomain", rows,
            last_complete_at=(_NOW - timedelta(hours=10)).isoformat(),
            now=_NOW,
        )
        assert h.status == health.STATUS_WARN

    def test_disabled_beats_down_too(self):
        """连续失败也不报——它没在跑，「失败」是停用前的历史。"""
        h = health.source_health_from_rows(
            "ourdomain", _rows(_fail(5)), last_complete_at="",
            now=_NOW, enabled=False,
        )
        assert h.status == health.STATUS_UNKNOWN

    def test_enabled_flag_is_exposed(self):
        h = health.source_health_from_rows("s", _rows(_ok(1)), enabled=False)
        assert h.as_dict()["enabled"] is False


class TestEnabledSourcesFromEnv:
    """``SOURCES`` 由 settings_store 每轮注水进环境，切分规则要和 config 对齐。"""

    @pytest.mark.parametrize("raw,want", [
        ("holland2stay,xior", {"holland2stay", "xior"}),
        ("holland2stay|xior", {"holland2stay", "xior"}),
        (" Holland2Stay , XIOR ", {"holland2stay", "xior"}),
        ("holland2stay,,xior", {"holland2stay", "xior"}),
    ])
    def test_parsing(self, monkeypatch, raw, want):
        monkeypatch.setenv("SOURCES", raw)
        assert health.enabled_sources() == want

    @pytest.mark.parametrize("raw", ["", "   ", ",", "|"])
    def test_unreadable_means_do_not_filter(self, monkeypatch, raw):
        """判断不了就别过滤——宁可多报，不可把真故障静音。"""
        monkeypatch.setenv("SOURCES", raw)
        assert health.enabled_sources() is None

    def test_missing_env_means_do_not_filter(self, monkeypatch):
        monkeypatch.delenv("SOURCES", raising=False)
        assert health.enabled_sources() is None


class TestWatchdogSkipsDisabledSources:
    def test_disabled_source_produces_no_alert(self, st, monkeypatch):
        monkeypatch.setenv("SOURCES", "holland2stay,xior")
        # ourdomain 早已停摆：全是完整轮，但最近一轮是 10 小时前
        st.record_round_stat(
            round_at=(_DB_NOW - timedelta(hours=10)).isoformat(),
            source="ourdomain", listings=12, targets=2, complete=2,
        )
        assert watchdog.poll(st) == []

    def test_enabled_source_in_the_same_db_still_alerts(self, st, monkeypatch):
        monkeypatch.setenv("SOURCES", "holland2stay,xior")
        st.record_round_stat(
            round_at=(_DB_NOW - timedelta(hours=10)).isoformat(),
            source="ourdomain", listings=12, targets=2, complete=2,
        )
        _seed(st, "holland2stay",
              [(284, 6, 6, "")] * 5 + [(0, 6, 6, "")] * 3)
        assert [a.key for a in watchdog.poll(st)] == ["source_zero:holland2stay"]

    def test_report_marks_disabled_sources(self, st, monkeypatch):
        monkeypatch.setenv("SOURCES", "xior")
        _seed(st, "xior", [(0, 4, 4, "")] * 3)
        _seed(st, "ourdomain", [(0, 2, 2, "")] * 3)
        by_src = {s["source"]: s for s in health.health_report(st)["sources"]}
        assert by_src["xior"]["enabled"] is True
        assert by_src["ourdomain"]["enabled"] is False
        assert by_src["ourdomain"]["status"] == health.STATUS_UNKNOWN
