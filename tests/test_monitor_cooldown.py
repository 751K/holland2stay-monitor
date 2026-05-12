"""
monitor.py 保护阀测试。

覆盖：
- _get_interval() 峰/谷判断逻辑
- _apply_jitter() 边界
- ScrapeNetworkError 连续失败计数与复位
- BlockedError 通知节流
"""
from __future__ import annotations

import time as _time
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest


# ── _apply_jitter ─────────────────────────────────────────

class TestApplyJitter:
    def test_within_range(self):
        from monitor import _apply_jitter
        for _ in range(100):
            result = _apply_jitter(100, 0.20)
            assert 80 <= result <= 120, f"jitter out of range: {result}"

    def test_floor_at_5_seconds(self):
        from monitor import _apply_jitter
        for _ in range(50):
            result = _apply_jitter(1, 0.20)
            assert result >= 5

    def test_zero_ratio_returns_exact(self):
        from monitor import _apply_jitter
        assert _apply_jitter(60, 0.0) == 60


# ── _get_interval ────────────────────────────────────────

@dataclass
class _FakeCfg:
    check_interval: int = 300
    peak_interval: int = 60
    peak_start: str = "08:30"
    peak_end: str = "10:00"
    peak_weekdays_only: bool = True
    min_interval: int = 15
    jitter_ratio: float = 0.20


class TestGetInterval:
    def test_peak_hours_return_peak_interval(self, monkeypatch):
        import monitor
        from zoneinfo import ZoneInfo
        import datetime as _dt

        ams = ZoneInfo("Europe/Amsterdam")
        fake_now = _dt.datetime(2026, 5, 13, 9, 0, 0, tzinfo=ams)  # Wed, peak

        monkeypatch.setattr(monitor, "datetime", MagicMock(now=lambda tz=None: fake_now))
        interval, is_peak = monitor._get_interval(_FakeCfg())
        assert is_peak is True
        assert interval == 60

    def test_off_peak_returns_check_interval(self, monkeypatch):
        import monitor
        from zoneinfo import ZoneInfo
        import datetime as _dt

        ams = ZoneInfo("Europe/Amsterdam")
        fake_now = _dt.datetime(2026, 5, 13, 14, 0, 0, tzinfo=ams)  # afternoon

        monkeypatch.setattr(monitor, "datetime", MagicMock(now=lambda tz=None: fake_now))
        interval, is_peak = monitor._get_interval(_FakeCfg())
        assert is_peak is False
        assert interval == 300

    def test_weekend_not_peak_when_weekdays_only(self, monkeypatch):
        import monitor
        from zoneinfo import ZoneInfo
        import datetime as _dt

        ams = ZoneInfo("Europe/Amsterdam")
        fake_now = _dt.datetime(2026, 5, 9, 9, 0, 0, tzinfo=ams)  # Saturday

        monkeypatch.setattr(monitor, "datetime", MagicMock(now=lambda tz=None: fake_now))
        _, is_peak = monitor._get_interval(_FakeCfg())
        assert is_peak is False


# ── _should_notify_block ──────────────────────────────────

class TestShouldNotifyBlock:
    def test_first_call_true(self, monkeypatch):
        from monitor import _should_notify_block
        monkeypatch.setattr("monitor._last_block_notify_at", 0.0)
        assert _should_notify_block() is True

    def test_second_call_within_interval_false(self, monkeypatch):
        from monitor import _should_notify_block
        now = _time.monotonic()
        monkeypatch.setattr("monitor._last_block_notify_at", now)
        assert _should_notify_block() is False

    def test_after_interval_true(self, monkeypatch):
        from monitor import _should_notify_block
        monkeypatch.setattr("monitor._last_block_notify_at", 0.0)
        monkeypatch.setattr(_time, "monotonic", lambda: 99999.0)
        assert _should_notify_block() is True


# ── ScrapeNetworkError cooldown threshold ─────────────────

class TestNetworkFailCooldown:
    def test_threshold_constant(self):
        from monitor import _NETWORK_FAIL_THRESHOLD, _NETWORK_FAIL_COOLDOWN
        assert _NETWORK_FAIL_THRESHOLD == 3
        assert _NETWORK_FAIL_COOLDOWN == 300


# ── BlockedError 冷却常量 ──────────────────────────────────

class TestBlockedCooldown:
    def test_blocked_cooldown_constant(self):
        from monitor import _BLOCKED_COOLDOWN
        assert _BLOCKED_COOLDOWN == 900

    def test_block_notify_interval(self):
        from monitor import _BLOCK_NOTIFY_INTERVAL
        assert _BLOCK_NOTIFY_INTERVAL == 1800
