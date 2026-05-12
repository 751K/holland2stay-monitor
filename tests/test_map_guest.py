"""
地图 API 访客只读逻辑测试。

回归保护：
- guest GET /api/map 不启动 geocode 线程、不写缓存
- guest POST /api/map/geocode 被拒绝
- admin POST /api/map/geocode 正常工作
"""
from __future__ import annotations

import threading
import time

import pytest


class TestGuestMapReadOnly:
    def test_guest_get_map_returns_data(self, guest_client):
        r = guest_client.get("/api/map")
        assert r.status_code == 200
        body = r.get_json()
        assert "listings" in body
        assert isinstance(body["listings"], list)

    def test_guest_get_map_has_uncached_field(self, guest_client):
        """uncached 字段用于前端提示，不应是 None 或缺失。"""
        r = guest_client.get("/api/map")
        body = r.get_json()
        assert "uncached" in body
        assert isinstance(body["uncached"], int)

    def test_guest_get_map_does_not_start_geocode_thread(self, guest_client, monkeypatch):
        """guest 访问 /api/map 不应触发后台 geocode 线程。"""
        from app.routes import map_routes
        map_routes._geocode_status["running"] = False
        map_routes._geocode_status["total"] = 0
        map_routes._geocode_status["done"] = 0
        map_routes._geocode_status["failed"] = 0

        spawned = []
        orig = threading.Thread
        def _track(*a, **kw):
            spawned.append(True)
            return orig(*a, **kw)
        monkeypatch.setattr(threading, "Thread", _track)

        guest_client.get("/api/map")
        # guest 不应启动任何线程
        assert len(spawned) == 0, f"Guest triggered {len(spawned)} geocode thread(s)"

    def test_guest_post_geocode_blocked(self, guest_client):
        r = guest_client.post("/api/map/geocode",
                              headers={"X-CSRF-Token": "test_csrf"})
        assert r.status_code == 403

    def test_anon_post_geocode_blocked(self, client):
        r = client.post("/api/map/geocode",
                         headers={"X-CSRF-Token": "test_csrf"})
        assert r.status_code == 401


class TestAdminGeocode:
    def test_admin_post_geocode_allowed(self, admin_client):
        r = admin_client.post("/api/map/geocode",
                              headers={"X-CSRF-Token": "test_csrf"})
        assert r.status_code == 200
        body = r.get_json()
        assert body.get("ok") is True

    def test_admin_post_geocode_requires_csrf(self, admin_client):
        r = admin_client.post("/api/map/geocode")
        assert r.status_code == 403
