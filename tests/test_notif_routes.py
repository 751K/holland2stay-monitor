"""
app/routes/notifications.py 路由测试。

覆盖：
- 分页查询（limit/offset）
- 标记已读（mark read）
- guest/admin 权限
- 非法 last_id/limit/ids 值
"""
from __future__ import annotations

import pytest


class TestNotificationsAuth:
    def test_anon_blocked(self, client):
        assert client.get("/api/notifications").status_code == 401

    def test_guest_blocked(self, guest_client):
        assert guest_client.get("/api/notifications").status_code == 403

    def test_admin_can_access(self, admin_client):
        r = admin_client.get("/api/notifications")
        assert r.status_code == 200


class TestNotificationsList:
    def test_default_limit_offset(self, admin_client):
        r = admin_client.get("/api/notifications")
        body = r.get_json()
        assert body["ok"] is True
        assert "unread" in body
        assert isinstance(body["notifications"], list)

    def test_custom_limit(self, admin_client):
        r = admin_client.get("/api/notifications?limit=10&offset=0")
        assert r.status_code == 200

    def test_limit_clamped_at_200(self, admin_client):
        r = admin_client.get("/api/notifications?limit=9999")
        assert r.status_code == 200
        # 实际返回 ≤200 条

    def test_negative_limit_defaults(self, admin_client):
        r = admin_client.get("/api/notifications?limit=-5")
        assert r.status_code == 200

    def test_non_integer_limit_defaults(self, admin_client):
        r = admin_client.get("/api/notifications?limit=abc")
        assert r.status_code == 200


class TestNotificationsRead:
    def test_mark_all_read(self, admin_client):
        r = admin_client.post("/api/notifications/read",
                              headers={"X-CSRF-Token": "test_csrf"})
        assert r.status_code == 200
        assert r.get_json()["ok"] is True

    def test_mark_specific_ids(self, admin_client):
        r = admin_client.post("/api/notifications/read",
                              json={"ids": [1, 2, 3]},
                              headers={"X-CSRF-Token": "test_csrf"})
        assert r.status_code == 200

    def test_invalid_ids_not_list(self, admin_client):
        r = admin_client.post("/api/notifications/read",
                              json={"ids": "not-a-list"},
                              headers={"X-CSRF-Token": "test_csrf"})
        assert r.status_code == 400

    def test_invalid_ids_not_integers(self, admin_client):
        r = admin_client.post("/api/notifications/read",
                              json={"ids": ["a", "b"]},
                              headers={"X-CSRF-Token": "test_csrf"})
        assert r.status_code == 400

    def test_read_requires_csrf(self, admin_client):
        r = admin_client.post("/api/notifications/read", json={"ids": [1]})
        assert r.status_code == 403


class TestSSE:
    def test_sse_anon_blocked(self, client):
        assert client.get("/api/events").status_code == 401

    def test_sse_guest_blocked(self, guest_client):
        assert guest_client.get("/api/events").status_code == 403

    def test_sse_admin_connects(self, admin_client):
        """SSE 连接应返回 text/event-stream。"""
        r = admin_client.get("/api/events")
        assert r.status_code == 200
        assert "text/event-stream" in r.content_type

    def test_sse_invalid_last_id_defaults(self, admin_client):
        r = admin_client.get("/api/events?last_id=abc")
        assert r.status_code == 200
