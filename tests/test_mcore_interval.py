"""mcore/interval.py 单元测试 — 不依赖 monitor.py。"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from mcore.interval import apply_jitter, get_interval


@dataclass
class _FakeCfg:
    check_interval: int = 300
    peak_interval: int = 60
    peak_start: str = "08:30"
    peak_end: str = "10:00"
    peak_start_2: str = "13:30"
    peak_end_2: str = "15:00"
    peak_weekdays_only: bool = True
    min_interval: int = 15
    jitter_ratio: float = 0.20


# ── get_interval ────────────────────────────────────────────────


class TestGetInterval:
    def test_peak_hours_weekday(self, monkeypatch):
        import datetime as _dt
        from zoneinfo import ZoneInfo

        import mcore.interval

        ams = ZoneInfo("Europe/Amsterdam")
        fake_now = _dt.datetime(2026, 5, 13, 9, 0, 0, tzinfo=ams)  # Wed 9:00

        monkeypatch.setattr(
            mcore.interval, "datetime", MagicMock(now=lambda tz=None: fake_now)
        )
        interval, is_peak = get_interval(_FakeCfg())
        assert is_peak is True
        assert interval == 60

    def test_off_peak_between_windows(self, monkeypatch):
        import datetime as _dt
        from zoneinfo import ZoneInfo

        import mcore.interval

        ams = ZoneInfo("Europe/Amsterdam")
        fake_now = _dt.datetime(2026, 5, 13, 11, 0, 0, tzinfo=ams)

        monkeypatch.setattr(
            mcore.interval, "datetime", MagicMock(now=lambda tz=None: fake_now)
        )
        interval, is_peak = get_interval(_FakeCfg())
        assert is_peak is False
        assert interval == 300

    def test_weekend_not_peak_when_weekdays_only(self, monkeypatch):
        import datetime as _dt
        from zoneinfo import ZoneInfo

        import mcore.interval

        ams = ZoneInfo("Europe/Amsterdam")
        fake_now = _dt.datetime(2026, 5, 9, 9, 0, 0, tzinfo=ams)  # Saturday

        monkeypatch.setattr(
            mcore.interval, "datetime", MagicMock(now=lambda tz=None: fake_now)
        )
        _, is_peak = get_interval(_FakeCfg())
        assert is_peak is False

    def test_weekend_is_peak_when_weekdays_only_disabled(self, monkeypatch):
        import datetime as _dt
        from zoneinfo import ZoneInfo

        import mcore.interval

        ams = ZoneInfo("Europe/Amsterdam")
        fake_now = _dt.datetime(2026, 5, 9, 9, 0, 0, tzinfo=ams)  # Saturday

        monkeypatch.setattr(
            mcore.interval, "datetime", MagicMock(now=lambda tz=None: fake_now)
        )
        cfg = _FakeCfg()
        cfg.peak_weekdays_only = False
        _, is_peak = get_interval(cfg)
        assert is_peak is True

    def test_second_peak_window(self, monkeypatch):
        import datetime as _dt
        from zoneinfo import ZoneInfo

        import mcore.interval

        ams = ZoneInfo("Europe/Amsterdam")
        fake_now = _dt.datetime(2026, 5, 13, 14, 0, 0, tzinfo=ams)  # Wed 14:00

        monkeypatch.setattr(
            mcore.interval, "datetime", MagicMock(now=lambda tz=None: fake_now)
        )
        _, is_peak = get_interval(_FakeCfg())
        assert is_peak is True

    def test_edge_of_window(self, monkeypatch):
        import datetime as _dt
        from zoneinfo import ZoneInfo

        import mcore.interval

        ams = ZoneInfo("Europe/Amsterdam")
        fake_now = _dt.datetime(2026, 5, 13, 10, 0, 0, tzinfo=ams)  # Wed 10:00

        monkeypatch.setattr(
            mcore.interval, "datetime", MagicMock(now=lambda tz=None: fake_now)
        )
        _, is_peak = get_interval(_FakeCfg())
        assert is_peak is True  # 10:00 仍在窗口内（≤）


# ── apply_jitter ──────────────────────────────────────────────────


class TestApplyJitter:
    def test_within_range(self):
        for _ in range(100):
            result = apply_jitter(100, 0.20)
            assert 80 <= result <= 120, f"jitter out of range: {result}"

    def test_floor_at_5_seconds(self):
        for _ in range(50):
            result = apply_jitter(1, 0.20)
            assert result >= 5

    def test_zero_ratio_returns_exact(self):
        assert apply_jitter(60, 0.0) == 60

    def test_zero_seconds(self):
        # With floor at 5, but if seconds=0 the delta=0 and returns max(5, 0) = 5
        result = apply_jitter(0, 0.20)
        assert result == 5

    def test_large_ratio(self):
        for _ in range(50):
            result = apply_jitter(100, 0.5)
            assert 50 <= result <= 150, f"jitter out of range with ratio=0.5: {result}"

    def test_deterministic_with_zero_ratio(self):
        # zero ratio must not add any randomness
        for _ in range(10):
            assert apply_jitter(60, 0.0) == 60
            assert apply_jitter(300, 0.0) == 300
