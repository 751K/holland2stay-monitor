"""Web 统计页 API 回归测试。"""
from __future__ import annotations


class TestStatsChartsApi:
    def test_charts_api_includes_range_summary(self, admin_client):
        r = admin_client.get("/api/charts?days=7")

        assert r.status_code == 200
        data = r.get_json()
        assert data["summary"]["days"] == 7
        assert "new_range" in data["summary"]
        assert "changes_range" in data["summary"]
        assert "city_dist" in data

    def test_web_and_public_api_charts_share_data(self, admin_client):
        days = 7
        web = admin_client.get(f"/api/charts?days={days}")
        assert web.status_code == 200
        web_data = web.get_json()

        index = admin_client.get("/api/v1/stats/public/charts")
        assert index.status_code == 200
        keys = index.get_json()["data"]["charts"]

        for key in keys:
            public = admin_client.get(f"/api/v1/stats/public/charts/{key}?days={days}")
            assert public.status_code == 200
            payload = public.get_json()["data"]
            assert payload["days"] == days
            assert payload["data"] == web_data[key]
