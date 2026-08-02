"""/health 的健康判定。

回归背景：2026-06-13 起 monitor 停了 7 周，容器全程报 healthy——因为当时
/health 只看 Web 能否响应。这里锁住「monitor 也纳入判定」以及几个不该误报
的场景。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import app.routes.system as system_routes


def _iso(delta_seconds: float) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(seconds=delta_seconds)
    ).isoformat()


@pytest.fixture
def fake_heartbeat(monkeypatch):
    """把心跳读取替换掉，避免依赖真实 DB 内容。"""
    def _set(age_seconds: float | None):
        monkeypatch.setattr(
            system_routes, "_heartbeat_age_seconds", lambda: age_seconds
        )
    return _set


def test_fresh_heartbeat_is_healthy(client, fake_heartbeat, monkeypatch):
    monkeypatch.setattr(system_routes, "is_monitor_running", lambda: True)
    fake_heartbeat(30)

    r = client.get("/health")

    assert r.status_code == 200
    assert r.get_json()["ok"] is True


def test_stale_heartbeat_reports_unhealthy(client, fake_heartbeat, monkeypatch):
    """被遗忘的暂停 / 卡死的循环必须暴露出来——这正是 7 周没被发现的场景。"""
    monkeypatch.setattr(system_routes, "is_monitor_running", lambda: False)
    fake_heartbeat(system_routes._HEARTBEAT_MAX_AGE + 60)

    r = client.get("/health")

    assert r.status_code == 503
    body = r.get_json()
    assert body["ok"] is False
    assert "心跳" in body["reason"]


def test_stale_heartbeat_unhealthy_even_if_process_alive(
    client, fake_heartbeat, monkeypatch
):
    """进程还在但循环卡死：PID 检查看不出来，心跳能。"""
    monkeypatch.setattr(system_routes, "is_monitor_running", lambda: True)
    fake_heartbeat(system_routes._HEARTBEAT_MAX_AGE + 60)

    r = client.get("/health")

    assert r.status_code == 503
    assert r.get_json()["monitor"] is True


def test_brief_pause_within_grace_stays_healthy(client, fake_heartbeat, monkeypatch):
    """管理员为部署 / 调试短暂停掉监控，不该立刻让容器翻红。"""
    monkeypatch.setattr(system_routes, "is_monitor_running", lambda: False)
    fake_heartbeat(system_routes._HEARTBEAT_MAX_AGE - 60)

    r = client.get("/health")

    assert r.status_code == 200


def test_missing_heartbeat_falls_back_to_process_check(
    client, fake_heartbeat, monkeypatch
):
    """全新部署 / 首轮未跑完时还没有心跳，不能因此误杀。"""
    fake_heartbeat(None)

    monkeypatch.setattr(system_routes, "is_monitor_running", lambda: True)
    assert client.get("/health").status_code == 200

    monkeypatch.setattr(system_routes, "is_monitor_running", lambda: False)
    assert client.get("/health").status_code == 503
