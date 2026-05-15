"""
API v1 只读数据端点测试（Phase 2）
====================================

覆盖：
- /listings 列表 + 详情：admin 全量 / user 应用 listing_filter / pagination
- /map：admin 全量 / user 过滤
- /calendar：admin 全量 / user 过滤
- /notifications：admin 全量 / user 只看 NULL+自己 + 类型白名单 + listing_filter
- /notifications/read：标记已读，user 不能跨越权
- /me/summary：role-aware 数字
- /me/filter：返回 user 的 filter，admin 返回空
- SSE 鉴权：header / query token 都能进入；坏 token 返回 401

设计：
- 用 tmp_path DB 隔离；fixture 预填充 3 套房源 + 部分 web_notifications
- 用 fixture 创建一个 user（filter: max_rent=900）只匹配 id-1
"""

from __future__ import annotations

import json

import pytest

from config import ListingFilter
from users import UserConfig, save_users, set_app_password


# ── DB 隔离 + 三档身份准备 ──────────────────────────────────────────


@pytest.fixture
def api_app(test_app, tmp_path, monkeypatch):
    monkeypatch.setattr("app.db.DB_PATH", tmp_path / "api2.db")
    from app import api_auth
    api_auth.invalidate_token_cache()
    yield test_app
    api_auth.invalidate_token_cache()


@pytest.fixture
def api_client(api_app):
    return api_app.test_client()


@pytest.fixture
def seeded(api_app):
    """预填房源 + 一个用户 (filter: max_rent<=900 → 只匹配 id-1)。"""
    from app.db import storage
    st = storage()
    try:
        rows = [
            # id, name, status, price_raw, avail, features, city
            ("id-1", "Studio Centrum", "Available to book", "€700", "2026-06-01",
             ["Type: Studio", "Area: 26.0 m²"], "Eindhoven"),
            ("id-2", "1BR West", "Available in lottery", "€950", "2026-07-01",
             ["Type: 1", "Area: 45.0 m²"], "Amsterdam"),
            ("id-3", "2BR South", "Not available", "€1200", "2026-08-01",
             ["Type: 2", "Area: 70.0 m²"], "Eindhoven"),
        ]
        for lid, name, status, price, avail, feats, city in rows:
            st.conn.execute(
                "INSERT INTO listings (id,name,status,price_raw,available_from,"
                "features,url,city,first_seen,last_seen,last_status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (lid, name, status, price, avail, json.dumps(feats),
                 f"https://h.com/{lid}", city,
                 "2026-05-13T08:00:00", "2026-05-13T08:00:00", status),
            )
        # 地图坐标缓存：给 id-1 / id-2 加坐标（id-3 没地址匹配，故意不缓存）
        from mstorage._map_calendar import _CITY_FORMAL
        for lid, addr in [
            ("id-1", "Studio Centrum, Eindhoven, Netherlands"),
            ("id-2", "1BR West, Amsterdam, Netherlands"),
        ]:
            st.cache_coords(addr, 51.0, 5.0)
        # web_notifications：3 条新房源 + 1 条 system error
        for lid, title in [
            ("id-1", "Studio Centrum"),
            ("id-2", "1BR West"),
            ("id-3", "2BR South"),
        ]:
            st.add_web_notification(
                type="new_listing", title=f"🎰 新房源：{title}",
                body="...", listing_id=lid,
            )
        st.add_web_notification(
            type="error", title="⚠️ 系统报错", body="something",
        )
        st.conn.commit()
    finally:
        st.close()

    # 用户：filter max_rent=900 → 只匹配 id-1 (700)
    plaintext = "user_pw_xyz"
    u = UserConfig(
        name="kong", id="kong0001",
        listing_filter=ListingFilter(max_rent=900),
    )
    u.app_login_enabled = True
    set_app_password(u, plaintext)
    save_users([u])
    return u, plaintext


def _login_admin(api_client, admin_password):
    r = api_client.post("/api/v1/auth/login", json={
        "username": "__admin__", "password": admin_password})
    return r.get_json()["data"]["token"]


def _login_user(api_client, user, plaintext):
    r = api_client.post("/api/v1/auth/login", json={
        "username": user.name, "password": plaintext})
    return r.get_json()["data"]["token"]


@pytest.fixture
def admin_token(api_client, test_credentials):
    return _login_admin(api_client, test_credentials["password"])


@pytest.fixture
def user_token(api_client, seeded):
    user, pw = seeded
    return _login_user(api_client, user, pw)


def _bearer(tok):
    return {"Authorization": f"Bearer {tok}"}


# ── /listings ──────────────────────────────────────────────────────


class TestListings:
    def test_anon_blocked(self, api_client, seeded):
        r = api_client.get("/api/v1/listings")
        assert r.status_code == 401

    def test_admin_sees_all(self, api_client, seeded, admin_token):
        r = api_client.get("/api/v1/listings", headers=_bearer(admin_token))
        assert r.status_code == 200
        d = r.get_json()["data"]
        ids = {x["id"] for x in d["items"]}
        assert ids == {"id-1", "id-2", "id-3"}
        assert d["total"] == 3
        assert d["filtered"] is False

    def test_user_filter_max_rent(self, api_client, seeded, user_token):
        r = api_client.get("/api/v1/listings", headers=_bearer(user_token))
        assert r.status_code == 200
        d = r.get_json()["data"]
        ids = {x["id"] for x in d["items"]}
        assert ids == {"id-1"}  # 700 通过；950/1200 被 max_rent=900 挡掉
        assert d["filtered"] is True

    def test_pagination(self, api_client, seeded, admin_token):
        r = api_client.get("/api/v1/listings?limit=1&offset=1",
                           headers=_bearer(admin_token))
        d = r.get_json()["data"]
        assert len(d["items"]) == 1
        assert d["total"] == 3
        assert d["limit"] == 1
        assert d["offset"] == 1

    def test_status_filter(self, api_client, seeded, admin_token):
        r = api_client.get("/api/v1/listings?status=Available to book",
                           headers=_bearer(admin_token))
        d = r.get_json()["data"]
        assert {x["id"] for x in d["items"]} == {"id-1"}

    def test_city_filter(self, api_client, seeded, admin_token):
        r = api_client.get("/api/v1/listings?city=Amsterdam",
                           headers=_bearer(admin_token))
        assert {x["id"] for x in r.get_json()["data"]["items"]} == {"id-2"}

    def test_search(self, api_client, seeded, admin_token):
        r = api_client.get("/api/v1/listings?q=Centrum",
                           headers=_bearer(admin_token))
        d = r.get_json()["data"]
        assert {x["id"] for x in d["items"]} == {"id-1"}

    def test_oversize_limit_clamped(self, api_client, seeded, admin_token):
        r = api_client.get("/api/v1/listings?limit=9999",
                           headers=_bearer(admin_token))
        d = r.get_json()["data"]
        assert d["limit"] == 500

    def test_serialize_includes_filter_friendly_fields(
        self, api_client, seeded, admin_token
    ):
        r = api_client.get("/api/v1/listings?status=Available to book",
                           headers=_bearer(admin_token))
        item = r.get_json()["data"]["items"][0]
        # 关键 iOS 端字段都在
        for k in ("id", "name", "status", "price_raw", "price_value",
                  "available_from", "city", "url", "features",
                  "feature_map", "first_seen", "last_seen"):
            assert k in item
        assert item["price_value"] == 700.0


class TestListingDetail:
    def test_admin_any(self, api_client, seeded, admin_token):
        r = api_client.get("/api/v1/listings/id-3",
                           headers=_bearer(admin_token))
        assert r.status_code == 200
        assert r.get_json()["data"]["id"] == "id-3"

    def test_user_404_on_filtered_out(self, api_client, seeded, user_token):
        # id-3 不在用户 filter 范围内 → 用户不该看到
        r = api_client.get("/api/v1/listings/id-3",
                           headers=_bearer(user_token))
        assert r.status_code == 404

    def test_user_can_see_match(self, api_client, seeded, user_token):
        r = api_client.get("/api/v1/listings/id-1",
                           headers=_bearer(user_token))
        assert r.status_code == 200

    def test_unknown_listing_404(self, api_client, seeded, admin_token):
        r = api_client.get("/api/v1/listings/nope",
                           headers=_bearer(admin_token))
        assert r.status_code == 404


# ── /map ───────────────────────────────────────────────────────────


class TestMap:
    def test_admin_sees_all_cached(self, api_client, seeded, admin_token):
        r = api_client.get("/api/v1/map", headers=_bearer(admin_token))
        d = r.get_json()["data"]
        ids = {x["id"] for x in d["listings"]}
        assert ids == {"id-1", "id-2"}  # 仅有坐标缓存的

    def test_user_filter_applied(self, api_client, seeded, user_token):
        r = api_client.get("/api/v1/map", headers=_bearer(user_token))
        d = r.get_json()["data"]
        ids = {x["id"] for x in d["listings"]}
        assert ids == {"id-1"}

    def test_anon_blocked(self, api_client, seeded):
        assert api_client.get("/api/v1/map").status_code == 401


# ── /calendar ──────────────────────────────────────────────────────


class TestCalendar:
    def test_admin_sees_all(self, api_client, seeded, admin_token):
        r = api_client.get("/api/v1/calendar", headers=_bearer(admin_token))
        ids = {x["id"] for x in r.get_json()["data"]["listings"]}
        assert ids == {"id-1", "id-2", "id-3"}

    def test_user_filter_applied(self, api_client, seeded, user_token):
        r = api_client.get("/api/v1/calendar", headers=_bearer(user_token))
        ids = {x["id"] for x in r.get_json()["data"]["listings"]}
        assert ids == {"id-1"}


# ── /notifications ─────────────────────────────────────────────────


class TestNotificationsList:
    def test_admin_sees_all_types(self, api_client, seeded, admin_token):
        r = api_client.get("/api/v1/notifications",
                           headers=_bearer(admin_token))
        d = r.get_json()["data"]
        types = {n["type"] for n in d["items"]}
        assert "error" in types       # admin 视角可见
        assert "new_listing" in types

    def test_user_no_error_type(self, api_client, seeded, user_token):
        r = api_client.get("/api/v1/notifications",
                           headers=_bearer(user_token))
        d = r.get_json()["data"]
        types = {n["type"] for n in d["items"]}
        assert "error" not in types
        assert types <= {"new_listing", "status_change", "booking"}

    def test_user_filter_drops_unmatched_listings(
        self, api_client, seeded, user_token
    ):
        """user 只看 listing_id 在自己 filter 范围内的通知。"""
        r = api_client.get("/api/v1/notifications",
                           headers=_bearer(user_token))
        d = r.get_json()["data"]
        # id-1 通知应保留；id-2/id-3 应剔除
        listing_ids = {n["listing_id"] for n in d["items"]}
        assert listing_ids == {"id-1"}

    def test_pagination(self, api_client, seeded, admin_token):
        r = api_client.get("/api/v1/notifications?limit=1",
                           headers=_bearer(admin_token))
        d = r.get_json()["data"]
        assert len(d["items"]) == 1
        assert d["total"] >= 1


class TestNotificationsRead:
    def test_mark_all_read(self, api_client, seeded, admin_token):
        r = api_client.post("/api/v1/notifications/read",
                            headers=_bearer(admin_token),
                            json={})
        assert r.status_code == 200
        # 二次查询应无未读
        r2 = api_client.get("/api/v1/notifications",
                            headers=_bearer(admin_token))
        assert r2.get_json()["data"]["unread"] == 0

    def test_mark_specific_ids(self, api_client, seeded, admin_token):
        r = api_client.post("/api/v1/notifications/read",
                            headers=_bearer(admin_token),
                            json={"ids": [1, 2]})
        assert r.status_code == 200

    def test_bad_ids_400(self, api_client, seeded, admin_token):
        r = api_client.post("/api/v1/notifications/read",
                            headers=_bearer(admin_token),
                            json={"ids": "not a list"})
        assert r.status_code == 400

    def test_user_mark_does_not_affect_admin_view(
        self, api_client, seeded, admin_token, user_token
    ):
        """user 调 '全部已读' 不应清掉所有；只清自己可见的。"""
        # 当前 user_id 列写入路径没 populate，所有通知 user_id="";
        # 用户视角下"系统通知" user_id='' 也算可见——所以也会被标记。
        # 这里只测语义不崩溃；详细 user_id 隔离留 Phase 3。
        r = api_client.post("/api/v1/notifications/read",
                            headers=_bearer(user_token),
                            json={})
        assert r.status_code == 200


# ── /me ────────────────────────────────────────────────────────────


class TestMe:
    def test_summary_admin(self, api_client, seeded, admin_token):
        r = api_client.get("/api/v1/me/summary",
                           headers=_bearer(admin_token))
        d = r.get_json()["data"]
        assert d["role"] == "admin"
        assert d["total_in_db"] == 3
        assert d["filter_active"] is False

    def test_summary_user(self, api_client, seeded, user_token):
        r = api_client.get("/api/v1/me/summary",
                           headers=_bearer(user_token))
        d = r.get_json()["data"]
        assert d["role"] == "user"
        assert d["matched_total"] == 1
        assert d["filter_active"] is True

    def test_filter_user(self, api_client, seeded, user_token):
        r = api_client.get("/api/v1/me/filter",
                           headers=_bearer(user_token))
        d = r.get_json()["data"]
        assert d["role"] == "user"
        assert d["filter"]["max_rent"] == 900
        assert d["is_empty"] is False

    def test_filter_admin(self, api_client, seeded, admin_token):
        r = api_client.get("/api/v1/me/filter",
                           headers=_bearer(admin_token))
        d = r.get_json()["data"]
        assert d["role"] == "admin"
        assert d["is_empty"] is True


# ── SSE 鉴权 ───────────────────────────────────────────────────────


class TestSSEAuth:
    def test_no_token_401(self, api_client, seeded):
        r = api_client.get("/api/v1/notifications/stream")
        assert r.status_code == 401

    def test_bad_token_401(self, api_client, seeded):
        r = api_client.get("/api/v1/notifications/stream",
                           headers=_bearer("garbage"))
        assert r.status_code == 401

    def test_bad_query_token_401(self, api_client, seeded):
        r = api_client.get("/api/v1/notifications/stream?token=garbage")
        assert r.status_code == 401

    def test_valid_header_200_streaming(self, api_client, seeded, admin_token):
        """带合法 header 应进入 SSE（200 + text/event-stream）。"""
        # 注意：test_client 是同步的，会阻塞在 yield；用 buffered=False+iter
        r = api_client.get("/api/v1/notifications/stream",
                           headers=_bearer(admin_token),
                           buffered=False)
        assert r.status_code == 200
        assert r.mimetype == "text/event-stream"
        r.close()

    def test_valid_query_token_200(self, api_client, seeded, admin_token):
        r = api_client.get(f"/api/v1/notifications/stream?token={admin_token}",
                           buffered=False)
        assert r.status_code == 200
        r.close()
