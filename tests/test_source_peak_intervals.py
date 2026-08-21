"""高峰期专用的 source 最小间隔。

为什么需要分时段
----------------
这道闸门**只在高峰期真正起作用**：

    峰外   轮次 = check_interval 300 秒起，加抖动实测中位 381 秒
           → 比 180 秒的闸门还慢，闸门形同不存在
    峰内   轮次 = peak_interval 60 秒，自适应还会衰减到 min_interval 20 秒
           → 闸门是唯一给 Xior 减速的东西

2026-08-14–21 七天实测（真实窗口 07:30–11:00 / 13:30–17:00，仅工作日）：

    峰内 180 闸   轮间隔中位 202 秒   每轮 429 概率 12.44%   51 次
    峰外 无闸     轮间隔中位 381 秒   每轮 429 概率  0.09%    1 次

节奏差不到 2 倍，429 概率差 138 倍——180 秒压在阈值悬崖边上，380 秒在安全侧。
98% 的 429 出在那 7 小时里。
"""
from __future__ import annotations

import pytest

import monitor
from scrapers.base import ScrapeTask
from storage import Storage


@pytest.fixture
def st(tmp_path):
    s = Storage(tmp_path / "t.db")
    yield s
    s.close()


class _Cfg:
    """够 _apply_source_intervals 和 get_interval 用的最小配置。"""

    def __init__(self, *, peak: bool, off=180, pk=360):
        self.source_min_intervals = {"xior": off} if off is not None else {}
        self.source_peak_min_intervals = {"xior": pk} if pk is not None else {}
        self.peak_weekdays_only = False
        # 让 get_interval 恒真 / 恒假：整天是峰，或整天不是
        self.peak_start, self.peak_end = ("00:00", "23:59") if peak else ("00:00", "00:00")
        self.peak_start_2, self.peak_end_2 = "00:00", "00:00"
        self.check_interval, self.peak_interval = 300, 60


def _tasks(n=2):
    return [ScrapeTask(source="xior", city_key=str(i), city_display=f"C{i}")
            for i in range(n)]


#: 时间基准不能用 0：``last <= 0`` 被 _apply_source_intervals 当作「从没抓过」
#: 而无条件放行，用 0 起步会让每个用例都走那条兜底分支。
_T0 = 1_000_000.0


def _gate(cfg, st, offset):
    return monitor._apply_source_intervals(_tasks(), cfg, st, now=_T0 + offset)


class TestPeakGateIsStricter:
    def test_off_peak_uses_the_regular_value(self, st):
        cfg = _Cfg(peak=False)
        assert _gate(cfg, st, 0.0)          # 首轮放行
        assert _gate(cfg, st, 100.0) == []  # < 180
        assert _gate(cfg, st, 200.0)        # > 180，放行

    def test_peak_uses_the_stricter_value(self, st):
        """同样是 200 秒，峰内要拦下来——峰内轮次快 10 倍，这是唯一的刹车。"""
        cfg = _Cfg(peak=True)
        assert _gate(cfg, st, 0.0)
        assert _gate(cfg, st, 200.0) == []  # 峰外会放行，峰内不行
        assert _gate(cfg, st, 400.0)        # > 360，放行

    def test_missing_peak_entry_falls_back(self, st):
        """没给某 source 配峰内值 = 「和平时一样」，不是「不限流」。"""
        cfg = _Cfg(peak=True, pk=None)
        assert _gate(cfg, st, 0.0)
        assert _gate(cfg, st, 100.0) == []  # 仍然按 180 拦
        assert _gate(cfg, st, 200.0)

    def test_peak_zero_still_means_off(self, st):
        """显式把峰内配成 0，就是峰内不节流——关掉就是关掉。"""
        cfg = _Cfg(peak=True, pk=0)
        for t in (0.0, 1.0, 2.0):
            assert _gate(cfg, st, t)


class TestComposesWithAdaptivePacing:
    def test_multiplier_applies_to_the_peak_base(self, st):
        """自适应倍率乘的是**当前时段那一档**，不是永远乘 180。"""
        cfg = _Cfg(peak=True)
        monitor._source_pacing.penalize("xior")      # ×2 → 360 × 2 = 720
        assert _gate(cfg, st, 0.0)
        assert _gate(cfg, st, 700.0) == []
        assert _gate(cfg, st, 800.0)

    def test_off_peak_multiplier_applies_to_the_regular_base(self, st):
        cfg = _Cfg(peak=False)
        monitor._source_pacing.penalize("xior")      # ×2 → 180 × 2 = 360
        assert _gate(cfg, st, 0.0)
        assert _gate(cfg, st, 300.0) == []
        assert _gate(cfg, st, 400.0)


class TestDefaults:
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
