"""
API v1 公共统计端点测试（guest 可访问）
=========================================

覆盖：
- guest（不带 token）能拿到 summary 和所有图表
- 带合法 admin/user token 也能拿到（bearer_optional 兼容）
- 带非法 token 仍然能拿到（fail-open，避免无效 token 卡住公开端点）
- 未知图表 key 返回 404
- days 参数边界 clamp 到 [1, 365]
- 响应壳形：{ok, data}
"""

from __future__ import annotations

import pytest


@pytest.fixture
def api_app(test_app, tmp_path, monkeypatch):
    fake_db = tmp_path / "stats_pub.db"
    monkeypatch.setattr("app.db.DB_PATH", fake_db)
    from app import api_auth
    api_auth.invalidate_token_cache()
    yield test_app
    api_auth.invalidate_token_cache()


@pytest.fixture
def api_client(api_app):
    return api_app.test_client()


# ── guest 访问 ─────────────────────────────────────────────────────


class TestGuestSummary:
    def test_no_token_ok(self, api_client):
        r = api_client.get("/api/v1/stats/public/summary")
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is True
        d = body["data"]
        for k in ("total", "new_24h", "new_7d", "changes_24h", "last_scrape"):
            assert k in d


class TestGuestChartsIndex:
    def test_no_token_lists_keys(self, api_client):
        r = api_client.get("/api/v1/stats/public/charts")
        assert r.status_code == 200
        keys = r.get_json()["data"]["charts"]
        assert "daily_new" in keys
        assert "status_dist" in keys
        # 12 张图都暴露
        assert len(keys) >= 10


class TestGuestChart:
    def test_default_days(self, api_client):
        r = api_client.get("/api/v1/stats/public/charts/daily_new")
        assert r.status_code == 200
        d = r.get_json()["data"]
        assert d["key"] == "daily_new"
        assert d["days"] == 30
        assert isinstance(d["data"], list)

    def test_custom_days(self, api_client):
        r = api_client.get("/api/v1/stats/public/charts/daily_new?days=7")
        assert r.get_json()["data"]["days"] == 7

    def test_days_clamped_high(self, api_client):
        r = api_client.get("/api/v1/stats/public/charts/daily_new?days=9999")
        assert r.get_json()["data"]["days"] == 365

    def test_days_clamped_low(self, api_client):
        r = api_client.get("/api/v1/stats/public/charts/daily_new?days=0")
        assert r.get_json()["data"]["days"] == 1

    def test_days_non_int(self, api_client):
        r = api_client.get("/api/v1/stats/public/charts/daily_new?days=foo")
        assert r.get_json()["data"]["days"] == 30  # 退回默认

    def test_unknown_key_404(self, api_client):
        r = api_client.get("/api/v1/stats/public/charts/nonexistent")
        assert r.status_code == 404
        assert r.get_json()["error"]["code"] == "not_found"


# ── 带 token 仍然可以访问 ───────────────────────────────────────────


class TestWithToken:
    def test_with_invalid_token_still_works(self, api_client):
        """bearer_optional：token 无效不应 401，guest 待遇即可。"""
        r = api_client.get(
            "/api/v1/stats/public/summary",
            headers={"Authorization": "Bearer garbage_token"},
        )
        assert r.status_code == 200

    def test_with_admin_token(self, api_client, test_credentials):
        # 先登录拿 token
        r = api_client.post("/api/v1/auth/login", json={
            "username": "__admin__",
            "password": test_credentials["password"],
        })
        token = r.get_json()["data"]["token"]
        r2 = api_client.get(
            "/api/v1/stats/public/summary",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r2.status_code == 200
