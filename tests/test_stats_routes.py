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
