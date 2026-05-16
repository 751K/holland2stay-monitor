"""
用户管理路由 E2E：/users + /users/new + /users/<id> + /users/<id>/toggle + /users/<id>/delete

T1 已经测了 build_user_from_form 的字段绑定。这里关心 HTTP 层 + 持久化：
- 创建 → users.json 写入
- 编辑 → 密码字段空提交 → 旧值保留（生产事故防线 E2E 版）
- 切换 enabled → 持久化
- 删除 → 移出 users.json
- 不存在的 user_id → flash + 重定向

isolated_data_dir fixture 把 USERS_FILE 重定向到 tmp_path，
所以测试不会污染真实 data/users.json。
"""
from __future__ import annotations

import pytest


def _create_user(admin_client, name="Test User", **extra_fields):
    """通过 POST /users/new 创建用户并返回新 user 实例。"""
    data = {
        "csrf_token": "test_csrf",
        "name": name,
        "enabled": "true",
        "NOTIFICATIONS_ENABLED": "true",
        "NOTIFICATION_CHANNELS": "imessage",
        "IMESSAGE_RECIPIENT": "+15550000000",
    }
    data.update(extra_fields)
    r = admin_client.post("/users/new", data=data)
    assert r.status_code == 302, f"create failed: {r.status_code} {r.get_data()[:200]}"
    from users import load_users
    users = load_users()
    return next(u for u in users if u.name == name)


# ─── List page ─────────────────────────────────────────────────


class TestUsersListPage:
    def test_empty_users_list_renders(self, admin_client):
        r = admin_client.get("/users")
        assert r.status_code == 200

    def test_guest_cannot_view_users(self, guest_client):
        r = guest_client.get("/users")
        assert r.status_code == 302
        assert r.headers["Location"].endswith("/")

    def test_anon_redirected_to_login(self, client):
        r = client.get("/users")
        assert r.status_code == 302
        assert "/login" in r.headers["Location"]


# ─── New user ──────────────────────────────────────────────────


class TestUserNew:
    def test_get_renders_blank_form(self, admin_client):
        r = admin_client.get("/users/new")
        assert r.status_code == 200

    def test_post_creates_user_and_persists(self, admin_client):
        from users import load_users
        # initially empty
        assert load_users() == []

        u = _create_user(admin_client, name="Alice", IMESSAGE_RECIPIENT="+15551111111")
        assert u.name == "Alice"
        assert u.imessage_recipient == "+15551111111"

        # Persisted on disk
        all_users = load_users()
        assert len(all_users) == 1
        assert all_users[0].id == u.id

    def test_csrf_required_for_create(self, admin_client):
        r = admin_client.post("/users/new", data={
            "name": "x",
            # no csrf_token
        })
        assert r.status_code == 403

    def test_guest_blocked_from_creating(self, guest_client):
        r = guest_client.post("/users/new", data={
            "csrf_token": "test_csrf", "name": "x",
        })
        assert r.status_code == 302  # redirect to /

    def test_create_rolls_back_when_app_user_sync_fails(self, admin_client, monkeypatch):
        from users import load_users
        from app.routes import users as users_route

        monkeypatch.setattr(
            users_route,
            "_sync_app_user_or_raise",
            lambda _user: (_ for _ in ()).throw(OSError("sync failed")),
        )
        r = admin_client.post("/users/new", data={
            "csrf_token": "test_csrf",
            "name": "Rollback Web",
            "enabled": "true",
            "NOTIFICATIONS_ENABLED": "true",
            "NOTIFICATION_CHANNELS": "imessage",
            "IMESSAGE_RECIPIENT": "+15550000000",
        })
        assert r.status_code == 302
        assert load_users() == []


# ─── Edit user + 密码保留 E2E ────────────────────────────────


class TestUserEdit:
    def test_edit_get_renders_form(self, admin_client):
        u = _create_user(admin_client, name="Bob")
        r = admin_client.get(f"/users/{u.id}")
        assert r.status_code == 200

    def test_edit_nonexistent_user_redirects(self, admin_client):
        r = admin_client.get("/users/nonexistentid")
        assert r.status_code == 302
        assert "/users" in r.headers["Location"]

    def test_edit_preserves_email_password_when_blank(self, admin_client):
        """E2E 版的密码保留契约：编辑表单空密码字段不能清空已存的密码。"""
        from users import load_users

        _create_user(admin_client, name="Carol",
                     EMAIL_PASSWORD="ORIGINAL_PW")
        u = next(x for x in load_users() if x.name == "Carol")
        assert u.email_password == "ORIGINAL_PW"

        # 编辑：改名，不动密码字段
        r = admin_client.post(f"/users/{u.id}", data={
            "csrf_token": "test_csrf",
            "name": "Carol Renamed",
            "enabled": "true",
            "NOTIFICATIONS_ENABLED": "true",
            "NOTIFICATION_CHANNELS": "imessage",
            # EMAIL_PASSWORD 故意不传
        })
        assert r.status_code == 302

        updated = next(x for x in load_users() if x.id == u.id)
        assert updated.name == "Carol Renamed"
        assert updated.email_password == "ORIGINAL_PW", \
            "密码字段在编辑时被错误清空 —— 生产事故！"

    def test_edit_preserves_twilio_token_when_blank(self, admin_client):
        from users import load_users
        _create_user(admin_client, name="Dave",
                     TWILIO_AUTH_TOKEN="TW_OLD")
        u = next(x for x in load_users() if x.name == "Dave")

        r = admin_client.post(f"/users/{u.id}", data={
            "csrf_token": "test_csrf", "name": "Dave",
            "NOTIFICATION_CHANNELS": "imessage",
        })
        assert r.status_code == 302
        updated = next(x for x in load_users() if x.id == u.id)
        assert updated.twilio_token == "TW_OLD"

    def test_edit_accepts_new_password(self, admin_client):
        """非空密码字段 → 接受新值。"""
        from users import load_users
        _create_user(admin_client, name="Eve", EMAIL_PASSWORD="OLD")
        u = next(x for x in load_users() if x.name == "Eve")

        admin_client.post(f"/users/{u.id}", data={
            "csrf_token": "test_csrf", "name": "Eve",
            "NOTIFICATION_CHANNELS": "imessage",
            "EMAIL_PASSWORD": "NEW",
        })
        updated = next(x for x in load_users() if x.id == u.id)
        assert updated.email_password == "NEW"

    def test_edit_preserves_user_id(self, admin_client):
        u = _create_user(admin_client, name="Frank")
        original_id = u.id

        admin_client.post(f"/users/{u.id}", data={
            "csrf_token": "test_csrf", "name": "Frank Renamed",
            "NOTIFICATION_CHANNELS": "imessage",
        })
        from users import load_users
        updated = load_users()[0]
        assert updated.id == original_id

    def test_edit_rolls_back_when_app_user_sync_fails(self, admin_client, monkeypatch):
        from users import load_users
        from app.routes import users as users_route

        u = _create_user(admin_client, name="Rollback Edit")
        monkeypatch.setattr(
            users_route,
            "_sync_app_user_or_raise",
            lambda _user: (_ for _ in ()).throw(OSError("sync failed")),
        )
        r = admin_client.post(f"/users/{u.id}", data={
            "csrf_token": "test_csrf",
            "name": "Rollback Edit Changed",
            "enabled": "true",
            "NOTIFICATIONS_ENABLED": "true",
            "NOTIFICATION_CHANNELS": "imessage",
        })
        assert r.status_code == 302
        users = load_users()
        assert len(users) == 1
        assert users[0].id == u.id
        assert users[0].name == "Rollback Edit"


# ─── Toggle ───────────────────────────────────────────────────


class TestUserToggle:
    def test_toggle_flips_enabled(self, admin_client):
        from users import load_users
        u = _create_user(admin_client, name="Grace")
        assert u.enabled is True

        r = admin_client.post(f"/users/{u.id}/toggle",
                              data={"csrf_token": "test_csrf"})
        assert r.status_code == 302

        toggled = load_users()[0]
        assert toggled.enabled is False

        # Toggle 回去
        admin_client.post(f"/users/{u.id}/toggle",
                          data={"csrf_token": "test_csrf"})
        assert load_users()[0].enabled is True

    def test_toggle_requires_csrf(self, admin_client):
        u = _create_user(admin_client, name="Heidi")
        r = admin_client.post(f"/users/{u.id}/toggle")  # no csrf
        assert r.status_code == 403

    def test_toggle_guest_blocked(self, admin_client, guest_client):
        u = _create_user(admin_client, name="Ivan")
        r = guest_client.post(f"/users/{u.id}/toggle",
                              data={"csrf_token": "test_csrf"})
        assert r.status_code == 302
        assert r.headers["Location"].endswith("/")


# ─── Delete ───────────────────────────────────────────────────


class TestUserDelete:
    def test_delete_removes_user(self, admin_client):
        from users import load_users
        u = _create_user(admin_client, name="Jack")
        assert len(load_users()) == 1

        r = admin_client.post(f"/users/{u.id}/delete",
                              data={"csrf_token": "test_csrf"})
        assert r.status_code == 302
        assert load_users() == []

    def test_delete_nonexistent_no_error(self, admin_client):
        """删除不存在的 user_id 不应该 500，只是 flash。"""
        r = admin_client.post("/users/ghostuser/delete",
                              data={"csrf_token": "test_csrf"})
        assert r.status_code == 302  # benign redirect

    def test_delete_only_removes_target_user(self, admin_client):
        from users import load_users
        u1 = _create_user(admin_client, name="K1")
        u2 = _create_user(admin_client, name="K2")
        u3 = _create_user(admin_client, name="K3")
        assert len(load_users()) == 3

        admin_client.post(f"/users/{u2.id}/delete",
                          data={"csrf_token": "test_csrf"})
        remaining = load_users()
        assert len(remaining) == 2
        assert {u.id for u in remaining} == {u1.id, u3.id}

    def test_delete_requires_csrf(self, admin_client):
        u = _create_user(admin_client, name="Liam")
        r = admin_client.post(f"/users/{u.id}/delete")
        assert r.status_code == 403
