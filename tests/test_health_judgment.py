"""数据健康判级 + 退化告警测试。

判级规则本身才是最容易写错的部分，所以主要用
``source_health_from_rows()`` 这个纯函数直接喂行、直接看级别，
不经过 DB。

最关键的一条契约：**「抓到 0 条」不等于「坏了」**。
Xior 四栋楼常态零可订，OurCampus 官网自述排队 16–18 个月。把它们钉在告警上
会让真信号被噪音淹掉，而告警一旦被无视，等于没有告警。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mcore import health, watchdog
from models import Listing
from storage import Storage


def _rows(specs):
    """specs 最新在前，每项 (listings, targets, complete, error_type)。"""
    return [
        {
            "round_at": f"2026-08-03T{20 - i:02d}:00:00+00:00",
            "source": "s",
            "listings": l, "targets": t, "complete": c,
            "duration_ms": 0, "error_type": e, "error_msg": "",
        }
        for i, (l, t, c, e) in enumerate(specs)
    ]


def _ok(n, listings=10):
    return [(listings, 2, 2, "")] * n


def _zero(n):
    return [(0, 2, 2, "")] * n


def _fail(n, err="RateLimitError"):
    return [(0, 2, 0, err)] * n


# ── 判级 ────────────────────────────────────────────────────────────


class TestJudgeStatus:
    def test_healthy_source_is_ok(self):
        h = health.source_health_from_rows("s", _rows(_ok(10)))
        assert h.status == health.STATUS_OK
        assert h.reasons == []

    def test_no_rows_is_unknown(self):
        h = health.source_health_from_rows("s", [])
        assert h.status == health.STATUS_UNKNOWN
        assert h.rounds == 0

    def test_consecutive_failures_is_down(self):
        h = health.source_health_from_rows("s", _rows(_fail(3) + _ok(5)))
        assert h.status == health.STATUS_DOWN
        assert h.fail_streak == 3
        assert h.last_error == "RateLimitError"

    def test_below_fail_threshold_is_not_down(self):
        h = health.source_health_from_rows("s", _rows(_fail(2) + _ok(5)))
        assert h.status != health.STATUS_DOWN

    def test_zero_after_nonzero_is_warn(self):
        """本来有房、突然全没了——上游改版打坏解析器就是这个特征。"""
        h = health.source_health_from_rows("s", _rows(_zero(3) + _ok(5, listings=284)))
        assert h.status == health.STATUS_WARN
        assert h.zero_streak == 3
        assert h.max_listings == 284

    def test_always_zero_source_stays_ok(self):
        """Xior / OurCampus 常态零可订，不该被永久钉在告警上。"""
        h = health.source_health_from_rows("s", _rows(_zero(20)))
        assert h.status == health.STATUS_OK
        assert h.zero_streak == 20
        assert h.max_listings == 0

    def test_low_completeness_is_warn(self):
        rows = _rows([(10, 6, 2, "")] * 5)   # 2/6 完整
        h = health.source_health_from_rows("s", rows)
        assert h.status == health.STATUS_WARN
        assert h.completeness_rate == pytest.approx(2 / 6)
        assert any("完整扫描率" in r for r in h.reasons)

    def test_full_completeness_is_ok(self):
        h = health.source_health_from_rows("s", _rows(_ok(5)))
        assert h.completeness_rate == pytest.approx(1.0)
        assert h.status == health.STATUS_OK


class TestStreaks:
    def test_fail_streak_breaks_on_success(self):
        h = health.source_health_from_rows("s", _rows(_fail(2) + _ok(1) + _fail(5)))
        assert h.fail_streak == 2

    def test_failed_round_breaks_zero_streak(self):
        """失败轮的 listings 恒为 0；若并入 zero_streak，任何一次失败都会顺带
        触发「零房源」告警，两条规则就重了。"""
        h = health.source_health_from_rows("s", _rows(_fail(1) + _zero(5) + _ok(3)))
        assert h.zero_streak == 0
        assert h.fail_streak == 1

    def test_last_success_and_nonzero_timestamps(self):
        rows = _rows(_fail(2) + _zero(1) + _ok(1, listings=7))
        h = health.source_health_from_rows("s", rows)
        assert h.last_success_at == rows[2]["round_at"]   # 第一条非 error
        assert h.last_nonzero_at == rows[3]["round_at"]   # 第一条 listings>0

    def test_averages_exclude_failed_rounds(self):
        """失败轮的 0 不该把平均值拉下来——那是「没抓」，不是「抓到 0 条」。"""
        h = health.source_health_from_rows("s", _rows(_fail(5) + _ok(2, listings=10)))
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


def _seed(st, source, specs):
    """specs 最旧在前，每项 (listings, targets, complete, error_type)。"""
    for i, (l, t, c, e) in enumerate(specs):
        st.record_round_stat(
            round_at=f"2026-08-03T{i:02d}:00:00+00:00", source=source,
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
        # 恢复：追加三轮成功
        for i in range(6, 9):
            st.record_round_stat(round_at=f"2026-08-03T{i:02d}:00:00+00:00",
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
        "round_at": "2026-08-03T10:00:00+00:00", "source": "s",
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
        h = health.source_health_from_rows("xior", rows)
        assert h.sharded is True
        assert h.zero_streak == 3          # 仍然如实统计
        assert h.status == health.STATUS_OK  # 但不据此告警

    def test_unsharded_zero_streak_still_warns(self):
        rows = [_row(0, 6, 6)] * 3 + [_row(284, 6, 6)]
        h = health.source_health_from_rows("holland2stay", rows)
        assert h.sharded is False
        assert h.status == health.STATUS_WARN

    def test_missing_total_targets_treated_as_unsharded(self):
        """老行没有这一列，保守按不分片处理，规则照常生效。"""
        rows = [_row(0, 6, 0)] * 3 + [_row(284, 6, 0)]
        h = health.source_health_from_rows("holland2stay", rows)
        assert h.sharded is False
        assert h.status == health.STATUS_WARN

    def test_sharded_still_reports_fail_streak(self):
        """分片只跳过零房源规则，连续失败照常判 down。"""
        rows = [_row(0, 3, 30, "RateLimitError")] * 3 + [_row(38, 3, 30)]
        h = health.source_health_from_rows("xior", rows)
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
