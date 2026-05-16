from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from users import UserConfig, load_users, set_app_password, update_users


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
    """并发 read-modify-write 不应互相覆盖 users.json。"""

    def add_user(i: int) -> None:
        def mutate(users):
            users.append(UserConfig(name=f"user-{i}", id=f"{i:08x}"))

        update_users(mutate)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(add_user, range(20)))

    users = load_users()
    assert len(users) == 20
    assert {u.name for u in users} == {f"user-{i}" for i in range(20)}


def test_app_users_unique_name(temp_db):
    temp_db.create_app_user(
        user_id="u1",
        name="alice",
        enabled=True,
        app_login_enabled=True,
        app_password_hash="hash1",
    )
    with pytest.raises(Exception) as exc:
        temp_db.create_app_user(
            user_id="u2",
            name="alice",
            enabled=True,
            app_login_enabled=True,
            app_password_hash="hash2",
        )
    assert temp_db.is_unique_violation(exc.value)


def test_concurrent_register_no_lost_users(api_register_app):
    """同时注册不同用户名时，SQLite 账号表和 users.json 都不能丢记录。"""

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
        for user in users:
            account = st.get_app_user_by_name(user.name)
            assert account is not None
            assert account["id"] == user.id
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


def test_register_rolls_back_users_json_when_sqlite_create_fails(api_register_app, monkeypatch):
    from app.routes.api_v1 import auth as auth_route

    def fail_create(*args, **kwargs):
        raise OSError("database is read-only")

    monkeypatch.setattr(auth_route.storage().__class__, "create_app_user", fail_create)

    with api_register_app.test_client() as client:
        r = client.post(
            "/api/v1/auth/register",
            json={
                "username": "rollback-me",
                "password": "pass-xyz",
                "device_name": "dev",
            },
        )

    assert r.status_code == 500
    assert [u.name for u in load_users()] == []


def test_login_reclaims_stale_sqlite_account_for_missing_config_id(api_register_app):
    """同名 SQLite 账号指向已不存在配置 id 时，登录会回收并同步当前配置。"""
    good = UserConfig(name="stale", id="good0001", enabled=True)
    good.app_login_enabled = True
    set_app_password(good, "good-pass")
    update_users(lambda users: users.append(good))

    from app.db import storage

    st = storage()
    try:
        st.create_app_user(
            user_id="old00001",
            name="stale",
            enabled=True,
            app_login_enabled=True,
            app_password_hash="not-a-real-hash",
        )
    finally:
        st.close()

    with api_register_app.test_client() as client:
        r = client.post(
            "/api/v1/auth/login",
            json={"username": "stale", "password": "good-pass", "device_name": "dev"},
        )

    assert r.status_code == 200
    assert r.get_json()["data"]["user_id"] == good.id
    st = storage()
    try:
        account = st.get_app_user_by_name("stale")
        assert account["id"] == good.id
    finally:
        st.close()


def test_login_rejects_conflicting_sqlite_account_when_old_config_exists(api_register_app):
    """同名 SQLite 账号指向另一个仍存在的配置用户时，不能签错 token。"""
    old = UserConfig(name="old-holder", id="old00002", enabled=True)
    current = UserConfig(name="conflict", id="new00002", enabled=True)
    current.app_login_enabled = True
    set_app_password(current, "current-pass")
    update_users(lambda users: users.extend([old, current]))

    from app.db import storage

    st = storage()
    try:
        st.create_app_user(
            user_id=old.id,
            name="conflict",
            enabled=True,
            app_login_enabled=True,
            app_password_hash=current.app_password_hash,
        )
    finally:
        st.close()

    with api_register_app.test_client() as client:
        r = client.post(
            "/api/v1/auth/login",
            json={"username": "conflict", "password": "current-pass", "device_name": "dev"},
        )

    assert r.status_code == 500
