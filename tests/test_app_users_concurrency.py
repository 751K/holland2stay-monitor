from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from users import UserConfig, load_users, update_users


@pytest.fixture
def api_register_app(test_app, tmp_path, monkeypatch):
    fake_db = tmp_path / "register.db"
    monkeypatch.setattr("app.db.DB_PATH", fake_db)

    from app import api_auth
    from app.routes.api_v1 import auth as auth_route

    api_auth.invalidate_token_cache()
    monkeypatch.setattr(auth_route.api_auth, "login_rate_check", lambda: (True, 0))
    monkeypatch.setattr(auth_route, "check_register_rate", lambda _ip: (True, ""))
    monkeypatch.setattr(auth_route, "record_registration", lambda _ip: None)
    yield test_app
    api_auth.invalidate_token_cache()


def test_update_users_serializes_concurrent_writes(isolated_data_dir):
    """并发 read-modify-write 不应互相覆盖 SQLite user_configs。"""

    def add_user(i: int) -> None:
        def mutate(users):
            users.append(UserConfig(name=f"user-{i}", id=f"{i:08x}"))

        update_users(mutate)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(add_user, range(20)))

    users = load_users()
    assert len(users) == 20
    assert {u.name for u in users} == {f"user-{i}" for i in range(20)}


def test_user_configs_unique_name(temp_db):
    temp_db.replace_user_config_rows([
        {
            "id": "u1",
            "name": "alice",
            "enabled": 1,
            "notifications_enabled": 1,
            "notification_channels_json": "[]",
            "listing_filter_json": "{}",
            "auto_book_json": "{}",
        }
    ])
    with pytest.raises(Exception) as exc:
        temp_db.replace_user_config_rows([
            {
                "id": "u1",
                "name": "alice",
                "enabled": 1,
                "notifications_enabled": 1,
                "notification_channels_json": "[]",
                "listing_filter_json": "{}",
                "auto_book_json": "{}",
            },
            {
                "id": "u2",
                "name": "alice",
                "enabled": 1,
                "notifications_enabled": 1,
                "notification_channels_json": "[]",
                "listing_filter_json": "{}",
                "auto_book_json": "{}",
            },
        ])
    assert temp_db.is_unique_violation(exc.value)


def test_schema_drops_legacy_app_users_table(temp_db):
    tables = {
        r[0]
        for r in temp_db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "user_configs" in tables
    assert "app_users" not in tables


def test_concurrent_register_no_lost_users(api_register_app):
    """同时注册不同用户名时，SQLite user_configs 不能丢记录。"""

    def register_one(i: int) -> int:
        with api_register_app.test_client() as client:
            r = client.post(
                "/api/v1/auth/register",
                json={
                    "username": f"reg-{i}",
                    "password": f"pass-{i}",
                    "device_name": f"dev-{i}",
                },
            )
            return r.status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        statuses = list(pool.map(register_one, range(8)))

    assert statuses == [201] * 8
    users = load_users()
    assert {u.name for u in users} == {f"reg-{i}" for i in range(8)}

    from app.db import storage

    st = storage()
    try:
        rows = st.list_user_config_rows()
        assert {r["name"] for r in rows} == {f"reg-{i}" for i in range(8)}
        assert {r["id"] for r in rows} == {u.id for u in users}
    finally:
        st.close()


def test_concurrent_register_same_name_allows_one(api_register_app):
    """同时注册同名用户时只能成功一个，不能写出重复配置。"""

    def register_one(_: int) -> int:
        with api_register_app.test_client() as client:
            r = client.post(
                "/api/v1/auth/register",
                json={
                    "username": "same-name",
                    "password": "pass-xyz",
                    "device_name": "dev",
                },
            )
            return r.status_code

    with ThreadPoolExecutor(max_workers=5) as pool:
        statuses = list(pool.map(register_one, range(5)))

    assert statuses.count(201) == 1
    assert statuses.count(409) == 4
    users = load_users()
    assert [u.name for u in users] == ["same-name"]
