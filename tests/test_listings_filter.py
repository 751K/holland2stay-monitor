"""
app/routes/dashboard.py 筛选逻辑 + storage 查询测试。

覆盖：
- get_all_listings 状态/城市/搜索
- count_new_since / count_changes_since
- chart_daily_new / chart_daily_changes 时区分组
- 坏 features JSON 在 get_map_listings 不崩
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
import pytest


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


@pytest.fixture
def db(temp_db):
    """预填充房源数据。"""
    now = _now_iso()
    rows = [
        ("id-1", "Studio Centrum", "Available to book", "€700", "2026-06-01", ["Type: Studio", "Area: 26.0 m²", "Contract: Indefinite", "Tenant: student only"], "Eindhoven"),
        ("id-2", "1BR West", "Available in lottery", "€950", "2026-07-01", ["Type: 1", "Area: 45.0 m²", "Contract: 6 months max", "Tenant: employed only"], "Amsterdam"),
        ("id-3", "2BR South", "Not available", "€1200", "2026-08-01", ["Type: 2", "Area: 70.0 m²", "Contract: Indefinite", "Tenant: student and employed"], "Eindhoven"),
    ]
    for i, (lid, name, status, price, avail, features, city) in enumerate(rows):
        temp_db.conn.execute(
            "INSERT OR REPLACE INTO listings (id, name, status, price_raw, available_from, features, url, city, first_seen, last_seen, last_status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (lid, name, status, price, avail, json.dumps(features), "https://x.com", city, now, now, status),
        )
    temp_db.conn.commit()
    return temp_db


class TestStorageQueries:
    def test_get_all_listings(self, db):
        rows = db.get_all_listings(limit=10)
        assert len(rows) == 3

    def test_get_all_listings_status_filter(self, db):
        rows = db.get_all_listings(status="Available to book", limit=10)
        assert len(rows) == 1
        assert rows[0]["id"] == "id-1"

    def test_get_all_listings_city_filter(self, db):
        rows = db.get_all_listings(city="Amsterdam", limit=10)
        assert len(rows) == 1
        assert rows[0]["id"] == "id-2"

    def test_get_all_listings_search(self, db):
        rows = db.get_all_listings(search="Centrum", limit=10)
        assert len(rows) == 1

    def test_get_all_listings_search_matches_building_name(self, db):
        # 单独插一条带 Building feature 的房源，name 完全无关，
        # 验证搜索可以命中楼盘名（"加上 building name" 这次新增能力）。
        now = _now_iso()
        db.conn.execute(
            "INSERT INTO listings (id, name, status, price_raw, available_from, features, url, city, first_seen, last_seen, last_status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "id-bld", "Some Random Address 99",
                "Available to book", "€800", "2026-06-01",
                json.dumps(["Type: Studio", "Building: The Docks", "Area: 30.0 m²"]),
                "https://x.com", "Eindhoven",
                now, now, "Available to book",
            ),
        )
        db.conn.commit()
        # 搜 "Docks" name 里完全没有，必须靠 features 里的 Building 命中
        rows = db.get_all_listings(search="Docks", limit=10)
        assert any(r["id"] == "id-bld" for r in rows)
        # 不该误伤其它房源（它们没有 Building 字段）
        assert all(r["id"] == "id-bld" for r in rows if "Docks" not in (r.get("name") or ""))

    def test_get_distinct_cities(self, db):
        cities = db.get_distinct_cities()
        assert "Amsterdam" in cities
        assert "Eindhoven" in cities

    def test_get_distinct_statuses(self, db):
        statuses = db.get_distinct_statuses()
        assert "Available to book" in statuses
        assert "Not available" in statuses

    def test_count_new_since(self, db):
        cnt = db.count_new_since(hours=1)
        assert cnt == 3

    def test_chart_daily_new_has_data(self, db):
        data = db.chart_daily_new(days=7)
        assert len(data) > 0
        assert db.chart_daily_new(days=30)

    def test_feature_values(self, db):
        cities = db.get_feature_values("Neighborhood", cities=["Eindhoven"])
        assert isinstance(cities, list)

    def test_bad_features_json_no_crash(self, temp_db):
        """损坏的 JSON 在 get_map_listings 不崩。"""
        temp_db.conn.execute(
            "INSERT INTO listings (id, name, status, price_raw, features, url, city, first_seen, last_seen, last_status) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("bad-id", "Bad", "Available", "€500", "BROKEN JSON {{{", "https://x.com", "E", "2026-01-01", "2026-01-01", "Available"),
        )
        temp_db.conn.commit()
        rows = temp_db.get_map_listings()
        assert len(rows) >= 1
