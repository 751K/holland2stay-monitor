"""
monitor.py 保护阀测试（interval / jitter 已迁至 test_mcore_interval.py）。

覆盖：
- _should_notify_block() 节流逻辑
- ScrapeNetworkError / BlockedError 冷却常量
"""
from __future__ import annotations

import time as _time

import pytest


# ── _should_notify_block ──────────────────────────────────

class TestShouldNotifyBlock:
    """节流窗口现在挂在 ``PersistedBackoff`` 上（落库、跨重启存活），
    不再是模块级 float —— 见 mcore/backoff.py。"""

    def test_first_call_true(self):
        import monitor
        monitor._throttle_notify_block.reset()
        assert monitor._should_notify_block() is True

    def test_second_call_within_interval_false(self):
        import monitor
        monitor._throttle_notify_block.reset()
        assert monitor._should_notify_block() is True
        assert monitor._should_notify_block() is False

    def test_after_interval_true(self):
        import monitor
        monitor._throttle_notify_block.reset()
        assert monitor._should_notify_block() is True
        # 窗口过期：把 deadline 拨到过去
        monitor._throttle_notify_block.expire()
        assert monitor._should_notify_block() is True


# ── ScrapeNetworkError cooldown ────────────────────────────

class TestNetworkFailCooldown:
    def test_threshold_constant(self):
        from monitor import _NETWORK_FAIL_THRESHOLD, _NETWORK_FAIL_COOLDOWN
        assert _NETWORK_FAIL_THRESHOLD == 3
        assert _NETWORK_FAIL_COOLDOWN == 300


# ── BlockedError cooldown ──────────────────────────────────

class TestBlockedCooldown:
    def test_blocked_cooldown_constant(self):
        from monitor import _BLOCKED_COOLDOWN
        assert _BLOCKED_COOLDOWN == 900

    def test_block_notify_interval(self):
        from monitor import _BLOCK_NOTIFY_INTERVAL
        assert _BLOCK_NOTIFY_INTERVAL == 1800
