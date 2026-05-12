"""
app/routes/control.py 路由测试。

覆盖：
- start/stop/reload/shutdown 权限和 CSRF
- monitor_pid()=None 时的 400/409 响应
- reload 文件触发回退路径
- shutdown 延迟 kill 线程
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ── 权限检查 ─────────────────────────────────────────────

class TestControlAuth:
    def test_anon_blocked_from_all(self, client):
        for url in ["/api/reload", "/api/monitor/start", "/api/monitor/stop", "/api/shutdown"]:
            r = client.post(url, headers={"X-CSRF-Token": "test_csrf"})
            assert r.status_code == 401, f"{url} returned {r.status_code}"

    def test_guest_blocked_from_all(self, guest_client):
        for url in ["/api/reload", "/api/monitor/start", "/api/monitor/stop", "/api/shutdown"]:
            r = guest_client.post(url, headers={"X-CSRF-Token": "test_csrf"})
            assert r.status_code == 403, f"{url} returned {r.status_code}"

    def test_csrf_required(self, admin_client):
        for url in ["/api/reload", "/api/monitor/start", "/api/monitor/stop", "/api/shutdown"]:
            r = admin_client.post(url)  # no CSRF header
            assert r.status_code == 403, f"{url} without CSRF returned {r.status_code}"


# ── reload ───────────────────────────────────────────────

class TestReload:
    def test_reload_no_monitor_returns_400(self, admin_client, monkeypatch):
        from app.routes import control as ctrl
        monkeypatch.setattr(ctrl, "monitor_pid", lambda: None)
        r = admin_client.post("/api/reload", headers={"X-CSRF-Token": "test_csrf"})
        assert r.status_code == 400
        assert "未运行" in r.get_json()["error"]

    def test_reload_file_fallback_on_signal_error(self, admin_client, monkeypatch):
        import signal
        from app.routes import control as ctrl

        monkeypatch.setattr(ctrl, "monitor_pid", lambda: 12345)
        # 模拟 os.kill 抛异常 → 回退到文件触发
        monkeypatch.setattr(ctrl.os, "kill", lambda pid, sig: (_ for _ in ()).throw(OSError("boom")))
        r = admin_client.post("/api/reload", headers={"X-CSRF-Token": "test_csrf"})
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is True
        assert "回退" in body["message"]


# ── start ────────────────────────────────────────────────

class TestStart:
    def test_start_no_monitor_succeeds(self, admin_client, monkeypatch):
        from app.routes import control as ctrl
        monkeypatch.setattr(ctrl, "monitor_pid", lambda: None)
        monkeypatch.setattr(ctrl.subprocess, "Popen", MagicMock())
        r = admin_client.post("/api/monitor/start", headers={"X-CSRF-Token": "test_csrf"})
        assert r.status_code == 200
        assert r.get_json()["ok"] is True

    def test_start_already_running_returns_409(self, admin_client, monkeypatch):
        from app.routes import control as ctrl
        monkeypatch.setattr(ctrl, "monitor_pid", lambda: 99999)
        r = admin_client.post("/api/monitor/start", headers={"X-CSRF-Token": "test_csrf"})
        assert r.status_code == 409


# ── stop ─────────────────────────────────────────────────

class TestStop:
    def test_stop_not_running_returns_409(self, admin_client, monkeypatch):
        from app.routes import control as ctrl
        monkeypatch.setattr(ctrl, "monitor_pid", lambda: None)
        r = admin_client.post("/api/monitor/stop", headers={"X-CSRF-Token": "test_csrf"})
        assert r.status_code == 409

    def test_stop_running_sends_sigterm(self, admin_client, monkeypatch):
        import signal
        from app.routes import control as ctrl
        monkeypatch.setattr(ctrl, "monitor_pid", lambda: 99999)
        kill_calls = []
        monkeypatch.setattr(ctrl.os, "kill", lambda pid, sig: kill_calls.append((pid, sig)))
        r = admin_client.post("/api/monitor/stop", headers={"X-CSRF-Token": "test_csrf"})
        assert r.status_code == 200
        assert kill_calls == [(99999, signal.SIGTERM)]

    def test_stop_kill_error_returns_500(self, admin_client, monkeypatch):
        from app.routes import control as ctrl
        monkeypatch.setattr(ctrl, "monitor_pid", lambda: 99999)
        monkeypatch.setattr(ctrl.os, "kill", lambda pid, sig: (_ for _ in ()).throw(OSError("boom")))
        r = admin_client.post("/api/monitor/stop", headers={"X-CSRF-Token": "test_csrf"})
        assert r.status_code == 500


# ── shutdown ─────────────────────────────────────────────

class TestShutdown:
    def test_shutdown_always_returns_200(self, admin_client, monkeypatch):
        from app.routes import control as ctrl
        monkeypatch.setattr(ctrl, "monitor_pid", lambda: None)
        with patch.object(ctrl.threading, "Thread", MagicMock()):
            r = admin_client.post("/api/shutdown", headers={"X-CSRF-Token": "test_csrf"})
        assert r.status_code == 200
        assert r.get_json()["ok"] is True
