"""
API v1 admin monitor control tests.

These endpoints share monitor process logic with the Web panel through
app.services.monitor_service, while keeping the API v1 response envelope.
"""

from __future__ import annotations

import pytest

from app.services.monitor_service import MonitorServiceError


@pytest.fixture
def api_app(test_app):
    from app import api_auth

    api_auth.invalidate_token_cache()
    yield test_app
    api_auth.invalidate_token_cache()


@pytest.fixture
def api_client(api_app):
    return api_app.test_client()


@pytest.fixture
def admin_token(api_client, test_credentials):
    r = api_client.post("/api/v1/auth/login", json={
        "username": "__admin__",
        "password": test_credentials["password"],
    })
    assert r.status_code == 200
    return r.get_json()["data"]["token"]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestAdminMonitor:
    def test_status_uses_service_payload(self, api_client, admin_token, monkeypatch):
        from app.routes.api_v1 import admin as admin_routes

        monkeypatch.setattr(admin_routes, "get_monitor_status", lambda: {
            "running": True,
            "pid": 123,
            "last_scrape": "2026-05-21T10:00:00",
            "last_count": "11",
        })

        r = api_client.get(
            "/api/v1/admin/monitor/status",
            headers=_bearer(admin_token),
        )

        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is True
        assert body["data"]["pid"] == 123

    def test_start_success(self, api_client, admin_token, monkeypatch):
        from app.routes.api_v1 import admin as admin_routes

        monkeypatch.setattr(
            admin_routes,
            "start_monitor",
            lambda: {"started": True, "method": "supervisor"},
        )

        r = api_client.post(
            "/api/v1/admin/monitor/start",
            headers=_bearer(admin_token),
        )

        assert r.status_code == 200
        assert r.get_json()["data"]["method"] == "supervisor"

    def test_start_conflict_preserves_validation_status(
        self, api_client, admin_token, monkeypatch
    ):
        from app.routes.api_v1 import admin as admin_routes

        def _raise():
            raise MonitorServiceError("监控已在运行", status=409, code="conflict")

        monkeypatch.setattr(admin_routes, "start_monitor", _raise)

        r = api_client.post(
            "/api/v1/admin/monitor/start",
            headers=_bearer(admin_token),
        )

        assert r.status_code == 400
        assert r.get_json()["error"]["code"] == "validation"

    def test_stop_conflict_preserves_validation_status(
        self, api_client, admin_token, monkeypatch
    ):
        from app.routes.api_v1 import admin as admin_routes

        def _raise():
            raise MonitorServiceError("监控未在运行", status=409, code="conflict")

        monkeypatch.setattr(admin_routes, "stop_monitor", _raise)

        r = api_client.post(
            "/api/v1/admin/monitor/stop",
            headers=_bearer(admin_token),
        )

        assert r.status_code == 400
        assert r.get_json()["error"]["code"] == "validation"

    def test_reload_not_running_returns_validation(
        self, api_client, admin_token, monkeypatch
    ):
        from app.routes.api_v1 import admin as admin_routes

        def _raise():
            raise MonitorServiceError("监控未在运行", status=400, code="validation")

        monkeypatch.setattr(admin_routes, "reload_monitor", _raise)

        r = api_client.post(
            "/api/v1/admin/monitor/reload",
            headers=_bearer(admin_token),
        )

        assert r.status_code == 400
        assert r.get_json()["error"]["code"] == "validation"

