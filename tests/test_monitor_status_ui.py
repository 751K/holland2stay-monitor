"""Web monitor paused/status UI tests."""
from __future__ import annotations


def test_api_status_exposes_paused_state(admin_client, monkeypatch):
    from app.services import monitor_service as svc

    monkeypatch.setattr(svc, "monitor_pid", lambda: None)

    r = admin_client.get("/api/status")
    assert r.status_code == 200
    body = r.get_json()
    assert body["running"] is False
    assert body["paused"] is True
    assert body["status"] == "paused"


def test_api_status_exposes_upstream_maintenance(admin_client, isolated_data_dir):
    from app.db import storage

    st = storage()
    try:
        st.set_meta("upstream_maintenance_seen_at", "2026-06-13T12:54:47+02:00")
        st.set_meta("upstream_maintenance_last_at", "2026-06-13T12:54:47+02:00")
    finally:
        st.close()

    r = admin_client.get("/api/status")
    assert r.status_code == 200
    body = r.get_json()
    assert body["upstream_maintenance"]["active"] is True
    assert body["upstream_maintenance"]["since"] == "2026-06-13T12:54:47+02:00"


def test_dashboard_shows_paused_banner_to_logged_in_users(admin_client, monkeypatch):
    from app.routes import dashboard as dashboard_route

    monkeypatch.setattr(dashboard_route, "monitor_pid", lambda: None)

    r = admin_client.get("/")
    assert r.status_code == 200
    html = r.data.decode()
    assert 'id="monitor-paused-banner"' in html
    assert "系统监控已暂停" in html or "Monitoring is paused" in html


def test_dashboard_renders_upstream_maintenance_banner(admin_client, isolated_data_dir):
    from app.db import storage
    import web

    st = storage()
    try:
        st.set_meta("upstream_maintenance_seen_at", "2026-06-13T12:54:47+02:00")
        st.set_meta("upstream_maintenance_last_at", "2026-06-13T12:54:47+02:00")
    finally:
        st.close()
    if hasattr(web._inject_upstream_maintenance, "_cache"):
        delattr(web._inject_upstream_maintenance, "_cache")

    r = admin_client.get("/")
    assert r.status_code == 200
    html = r.data.decode()
    assert 'id="upstream-maintenance-banner"' in html
    assert "Holland2Stay 平台维护中" in html or "Holland2Stay under maintenance" in html


def test_system_info_has_admin_monitor_controls(admin_client, monkeypatch):
    from app.routes import system as system_route

    monkeypatch.setattr(system_route, "monitor_pid", lambda: None)

    r = admin_client.get("/system")
    assert r.status_code == 200
    html = r.data.decode()
    assert 'id="monitor-start-btn"' in html
    assert 'id="monitor-stop-btn"' in html
    assert 'id="monitor-restart-btn"' in html
    assert "系统暂停" in html or "System paused" in html
