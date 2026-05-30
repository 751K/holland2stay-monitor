"""
API v1 条件缓存测试（ETag + Cache-Control + 304）
==================================================

验证 ``app/routes/api_v1/__init__.py:_apply_conditional_cache``：
- GET 200 JSON → 带 ETag + Cache-Control: private, max-age=10, must-revalidate
- 重复请求带 If-None-Match 命中 → 304（无 body）
- ETag 随内容变化（内容变 → 新 ETag → 不再 304）
- POST / 错误响应 / 非 JSON 不加缓存头
"""
from __future__ import annotations

import pytest


@pytest.fixture
def api_app(test_app, tmp_path, monkeypatch):
    fake_db = tmp_path / "cache.db"
    monkeypatch.setattr("app.db.DB_PATH", fake_db)
    from app import api_auth
    api_auth.invalidate_token_cache()
    yield test_app
    api_auth.invalidate_token_cache()


@pytest.fixture
def api_client(api_app):
    return api_app.test_client()


# 用 guest 可访问的公开端点测——无需鉴权，最稳
_PUBLIC_GET = "/api/v1/stats/public/summary"


class TestConditionalCache:
    def test_get_has_etag_and_cache_control(self, api_client):
        r = api_client.get(_PUBLIC_GET)
        assert r.status_code == 200
        assert r.headers.get("ETag", "").startswith('"')
        cc = r.headers.get("Cache-Control", "")
        assert "private" in cc
        assert "max-age=10" in cc
        assert "must-revalidate" in cc

    def test_if_none_match_returns_304(self, api_client):
        r1 = api_client.get(_PUBLIC_GET)
        etag = r1.headers["ETag"]

        r2 = api_client.get(_PUBLIC_GET, headers={"If-None-Match": etag})
        assert r2.status_code == 304
        assert r2.get_data() == b""
        # 304 仍带同一 ETag，便于客户端继续复用
        assert r2.headers.get("ETag") == etag

    def test_stale_etag_returns_full_200(self, api_client):
        r = api_client.get(_PUBLIC_GET, headers={"If-None-Match": '"deadbeef"'})
        assert r.status_code == 200
        assert r.get_data()  # 有 body

    def test_etag_changes_when_content_changes(self, api_client, monkeypatch):
        r1 = api_client.get(_PUBLIC_GET)
        etag1 = r1.headers["ETag"]

        # 改变后端返回内容 → ETag 必须变 → 同一 If-None-Match 不再命中
        # 直接 patch 一个会改变 summary 数字的底层（这里用 stats_service）
        # 简单做法：插入一条 listing 改变 total 计数
        from app.db import storage
        from models import Listing
        st = storage()
        try:
            st.diff([Listing(
                id="cache-test-1", name="X", status="Available to book",
                price_raw="€999", available_from="2030-01-01",
                features=[], url="https://x", city="Eindhoven",
            )])
        finally:
            st.close()

        r2 = api_client.get(_PUBLIC_GET, headers={"If-None-Match": etag1})
        # 内容变了 → 不应再 304
        assert r2.status_code == 200
        assert r2.headers["ETag"] != etag1

    def test_error_response_not_cached(self, api_client):
        # 未知图表 key → 404，不应带 ETag
        r = api_client.get("/api/v1/stats/public/chart/__nope__")
        assert r.status_code == 404
        assert "ETag" not in r.headers

    def test_post_not_cached(self, api_client):
        # 登录端点是 POST；即便 4xx 也不应有 ETag
        r = api_client.post("/api/v1/auth/login", json={"username": "x", "password": "y"})
        assert "ETag" not in r.headers

    def test_public_endpoint_uses_max_age(self, api_client):
        """非通知端点用 max-age（快速切 tab 零网络）。"""
        r = api_client.get(_PUBLIC_GET)
        assert "max-age=10" in r.headers.get("Cache-Control", "")
        assert "no-cache" not in r.headers.get("Cache-Control", "")

