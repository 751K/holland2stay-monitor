"""按 source 节流：控制「同一个 target 多久被打一次」。

和分片解决的**不是同一个问题**——分片管「每轮抓几个 target」。2026-08-04
生产实测把两者混为一谈的代价：

Xior 从 30 栋缩到 4 栋后，分片 3/轮 等于每栋楼几乎每轮都被抓；而高峰时段轮次
间隔只有 60–90 秒，于是单栋楼的请求频率从「每 10–15 分钟一次」涨到「每 60–90
秒一次」，约 10 倍，直接撞进限流：持续 429、退避 30s+60s，单轮从 40 秒拖到
270 秒。

**楼栋数变少反而更容易被限流**，因为限流按单个 target 被打的频率算——30 栋
轮着抓时每栋自然稀疏，4 栋轮着抓就全挤在一起了。分片再怎么调都救不了，它压根
不控制频率。
"""
from __future__ import annotations

import pytest

import monitor
from monitor import _SOURCE_LAST_SCRAPE_PREFIX, _apply_source_intervals
from scrapers.base import ScrapeTask
from storage import Storage


class _Task:
    def __init__(self, source, city="c"):
        self.source = source
        self.city_display = city


class _Cfg:
    def __init__(self, intervals):
        self.source_min_intervals = intervals


class _Meta:
    """只实现 get_meta / set_meta 的假 storage。"""

    def __init__(self, initial=None, fail_read=False, fail_write=False):
        self.data = dict(initial or {})
        self.fail_read = fail_read
        self.fail_write = fail_write

    def get_meta(self, key, default=""):
        if self.fail_read:
            raise RuntimeError("meta 读挂了")
        return self.data.get(key, default)

    def set_meta(self, key, value):
        if self.fail_write:
            raise RuntimeError("meta 写挂了")
        self.data[key] = value


def _tasks():
    return [_Task("xior", "A"), _Task("xior", "B"), _Task("holland2stay", "E")]


class TestThrottling:
    def test_skips_a_source_scraped_too_recently(self):
        st = _Meta({_SOURCE_LAST_SCRAPE_PREFIX + "xior": "1000"})
        out = _apply_source_intervals(_tasks(), _Cfg({"xior": 600}), st, now=1100)
        assert [t.source for t in out] == ["holland2stay"], "xior 应被整体跳过"

    def test_runs_once_the_gap_has_passed(self):
        st = _Meta({_SOURCE_LAST_SCRAPE_PREFIX + "xior": "1000"})
        out = _apply_source_intervals(_tasks(), _Cfg({"xior": 600}), st, now=1700)
        assert sorted({t.source for t in out}) == ["holland2stay", "xior"]

    def test_other_sources_are_untouched(self):
        """节流是逐 source 的——H2S 靠高频轮次出房源，不能被 Xior 拖累。"""
        st = _Meta({_SOURCE_LAST_SCRAPE_PREFIX + "xior": "1000"})
        out = _apply_source_intervals(_tasks(), _Cfg({"xior": 600}), st, now=1001)
        assert [t.city_display for t in out] == ["E"]

    def test_first_ever_round_is_not_skipped(self):
        out = _apply_source_intervals(_tasks(), _Cfg({"xior": 600}), _Meta(), now=50)
        assert len(out) == 3

    def test_zero_disables_throttling(self):
        st = _Meta({_SOURCE_LAST_SCRAPE_PREFIX + "xior": "1000"})
        out = _apply_source_intervals(_tasks(), _Cfg({"xior": 0}), st, now=1001)
        assert len(out) == 3

    def test_no_config_is_a_no_op(self):
        assert len(_apply_source_intervals(_tasks(), _Cfg({}), _Meta(), now=1)) == 3


class TestTimestampPersistence:
    def test_records_the_time_it_actually_ran(self):
        st = _Meta()
        _apply_source_intervals(_tasks(), _Cfg({"xior": 600}), st, now=1234)
        assert st.data[_SOURCE_LAST_SCRAPE_PREFIX + "xior"] == "1234"

    def test_skipped_round_does_not_refresh_the_timestamp(self):
        """否则每次跳过都把闸门往后推，source 会被永久饿死。"""
        st = _Meta({_SOURCE_LAST_SCRAPE_PREFIX + "xior": "1000"})
        _apply_source_intervals(_tasks(), _Cfg({"xior": 600}), st, now=1100)
        assert st.data[_SOURCE_LAST_SCRAPE_PREFIX + "xior"] == "1000"

    def test_dry_run_does_not_write(self):
        st = _Meta()
        _apply_source_intervals(_tasks(), _Cfg({"xior": 600}), st, now=5, dry_run=True)
        assert st.data == {}


class TestFailOpen:
    """读写 meta 出问题时宁可多抓一轮，也不能把整个 source 静默停掉。"""

    def test_unreadable_meta_still_scrapes(self):
        out = _apply_source_intervals(
            _tasks(), _Cfg({"xior": 600}), _Meta(fail_read=True), now=1,
        )
        assert len(out) == 3

    def test_unwritable_meta_still_scrapes(self):
        out = _apply_source_intervals(
            _tasks(), _Cfg({"xior": 600}), _Meta(fail_write=True), now=1,
        )
        assert len(out) == 3

    def test_garbage_timestamp_is_treated_as_never(self):
        st = _Meta({_SOURCE_LAST_SCRAPE_PREFIX + "xior": "not-a-number"})
        assert len(_apply_source_intervals(_tasks(), _Cfg({"xior": 600}), st, now=1)) == 3

    def test_future_timestamp_does_not_wedge_the_source(self):
        """时钟回拨过一次，不该让 source 卡到时间追上为止。"""
        st = _Meta({_SOURCE_LAST_SCRAPE_PREFIX + "xior": "99999999"})
        assert len(_apply_source_intervals(_tasks(), _Cfg({"xior": 600}), st, now=1000)) == 3


class TestDefaults:
    def test_xior_is_throttled_by_default(self):
        """默认值要能直接修好生产上那个问题，不该等人去配。

        下界守的是「明显比轮次间隔松」——实测每栋楼 60–90 秒一次会持续吃
        429，单轮从 40 秒拖到 274 秒。具体数值会按实测继续调，所以这里不钉
        死某个数。
        """
        from config import _DEFAULT_SOURCE_MIN_INTERVALS

        assert _DEFAULT_SOURCE_MIN_INTERVALS.get("xior", 0) >= 180

    def test_other_sources_have_no_default_throttle(self):
        """H2S 是真正出房源的那个，高峰高频轮询是有意为之。"""
        from config import _DEFAULT_SOURCE_MIN_INTERVALS

        assert "holland2stay" not in _DEFAULT_SOURCE_MIN_INTERVALS


# ── 高峰期专用的 source 最小间隔（原 test_source_peak_intervals.py） ──
#
# 为什么需要分时段
# ----------------
# 这道闸门**只在高峰期真正起作用**：
#
#     峰外   轮次 = check_interval 300 秒起，加抖动实测中位 381 秒
#            → 比 180 秒的闸门还慢，闸门形同不存在
#     峰内   轮次 = peak_interval 60 秒，自适应还会衰减到 min_interval 20 秒
#            → 闸门是唯一给 Xior 减速的东西
#
# 2026-08-14–21 七天实测（真实窗口 07:30–11:00 / 13:30–17:00，仅工作日）：
#
#     峰内 180 闸   轮间隔中位 202 秒   每轮 429 概率 12.44%   51 次
#     峰外 无闸     轮间隔中位 381 秒   每轮 429 概率  0.09%    1 次
#
# 节奏差不到 2 倍，429 概率差 138 倍——180 秒压在阈值悬崖边上，380 秒在安全侧。
# 98% 的 429 出在那 7 小时里。

@pytest.fixture
def st(tmp_path):
    s = Storage(tmp_path / "t.db")
    yield s
    s.close()


class _PeakCfg:
    """够 _apply_source_intervals 和 get_interval 用的最小配置。"""

    def __init__(self, *, peak: bool, off=180, pk=360):
        self.source_min_intervals = {"xior": off} if off is not None else {}
        self.source_peak_min_intervals = {"xior": pk} if pk is not None else {}
        self.peak_weekdays_only = False
        # 让 get_interval 恒真 / 恒假：整天是峰，或整天不是
        self.peak_start, self.peak_end = ("00:00", "23:59") if peak else ("00:00", "00:00")
        self.peak_start_2, self.peak_end_2 = "00:00", "00:00"
        self.check_interval, self.peak_interval = 300, 60


def _peak_tasks(n=2):
    return [ScrapeTask(source="xior", city_key=str(i), city_display=f"C{i}")
            for i in range(n)]


#: 时间基准不能用 0：``last <= 0`` 被 _apply_source_intervals 当作「从没抓过」
#: 而无条件放行，用 0 起步会让每个用例都走那条兜底分支。
_PEAK_T0 = 1_000_000.0


def _gate(cfg, st, offset):
    return monitor._apply_source_intervals(
        _peak_tasks(), cfg, st, now=_PEAK_T0 + offset)


class TestPeakGateIsStricter:
    def test_off_peak_uses_the_regular_value(self, st):
        cfg = _PeakCfg(peak=False)
        assert _gate(cfg, st, 0.0)          # 首轮放行
        assert _gate(cfg, st, 100.0) == []  # < 180
        assert _gate(cfg, st, 200.0)        # > 180，放行

    def test_peak_uses_the_stricter_value(self, st):
        """同样是 200 秒，峰内要拦下来——峰内轮次快 10 倍，这是唯一的刹车。"""
        cfg = _PeakCfg(peak=True)
        assert _gate(cfg, st, 0.0)
        assert _gate(cfg, st, 200.0) == []  # 峰外会放行，峰内不行
        assert _gate(cfg, st, 400.0)        # > 360，放行

    def test_missing_peak_entry_falls_back(self, st):
        """没给某 source 配峰内值 = 「和平时一样」，不是「不限流」。"""
        cfg = _PeakCfg(peak=True, pk=None)
        assert _gate(cfg, st, 0.0)
        assert _gate(cfg, st, 100.0) == []  # 仍然按 180 拦
        assert _gate(cfg, st, 200.0)

    def test_peak_zero_still_means_off(self, st):
        """显式把峰内配成 0，就是峰内不节流——关掉就是关掉。"""
        cfg = _PeakCfg(peak=True, pk=0)
        for t in (0.0, 1.0, 2.0):
            assert _gate(cfg, st, t)


class TestComposesWithAdaptivePacing:
    def test_multiplier_applies_to_the_peak_base(self, st):
        """自适应倍率乘的是**当前时段那一档**，不是永远乘 180。"""
        cfg = _PeakCfg(peak=True)
        monitor._source_pacing.penalize("xior")      # ×2 → 360 × 2 = 720
        assert _gate(cfg, st, 0.0)
        assert _gate(cfg, st, 700.0) == []
        assert _gate(cfg, st, 800.0)

    def test_off_peak_multiplier_applies_to_the_regular_base(self, st):
        cfg = _PeakCfg(peak=False)
        monitor._source_pacing.penalize("xior")      # ×2 → 180 × 2 = 360
        assert _gate(cfg, st, 0.0)
        assert _gate(cfg, st, 300.0) == []
        assert _gate(cfg, st, 400.0)


class TestPeakDefaults:
    def test_shipped_defaults_match_the_measurement(self):
        """峰内默认 360 秒，贴着实测安全的 381 秒。"""
        from config import (_DEFAULT_SOURCE_MIN_INTERVALS,
                            _DEFAULT_SOURCE_PEAK_MIN_INTERVALS)
        assert _DEFAULT_SOURCE_MIN_INTERVALS["xior"] == 180
        assert _DEFAULT_SOURCE_PEAK_MIN_INTERVALS["xior"] == 360

    def test_env_override_parses(self, monkeypatch):
        monkeypatch.setenv("SOURCE_PEAK_MIN_INTERVALS", "xior:600,ourdomain:90")
        import config
        got = config._parse_source_min_intervals(
            "xior:600,ourdomain:90", config._DEFAULT_SOURCE_PEAK_MIN_INTERVALS)
        assert got["xior"] == 600
        assert got["ourdomain"] == 90

    def test_empty_env_keeps_defaults(self):
        import config
        got = config._parse_source_min_intervals(
            "", config._DEFAULT_SOURCE_PEAK_MIN_INTERVALS)
        assert got == config._DEFAULT_SOURCE_PEAK_MIN_INTERVALS

    def test_regular_defaults_are_not_mutated(self):
        """两份默认值不能共享同一个 dict——改一份会污染另一份。"""
        import config
        a = config._parse_source_min_intervals("xior:1")
        b = config._parse_source_min_intervals(
            "xior:2", config._DEFAULT_SOURCE_PEAK_MIN_INTERVALS)
        assert a["xior"] == 1 and b["xior"] == 2
        assert config._DEFAULT_SOURCE_MIN_INTERVALS["xior"] == 180
        assert config._DEFAULT_SOURCE_PEAK_MIN_INTERVALS["xior"] == 360
