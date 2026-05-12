"""
storage.py 图表统计 + 坐标缓存测试。

覆盖：
- chart_energy_dist 排序（A+++ → F）
- 坏 features JSON 跳过不崩
- chart_area_dist / chart_floor_dist 桶边界
- get_cached_coords / cache_coords round-trip
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture
def db(temp_db):
    """填充测试数据的 Storage。"""
    db = temp_db
    rows = [
        ("Available to book", "2026-05-13T10:00:00", ["Type: Studio", "Energy: A", "Area: 25.0 m²", "Floor: 3"]),
        ("Available in lottery", "2026-05-13T09:00:00", ["Type: 1", "Energy: B", "Area: 45.0 m²", "Floor: 1"]),
        ("Available to book", "2026-05-13T08:00:00", ["Type: 2", "Energy: C", "Area: 65.0 m²", "Floor: 0"]),
        ("Not available", "2026-05-13T11:00:00", ["Type: Loft", "Energy: A++", "Area: 90.0 m²", "Floor: 8"]),
    ]
    for i, (status, first_seen, features) in enumerate(rows):
        features_json = json.dumps(features)
        db._conn.execute(
            "INSERT OR REPLACE INTO listings (id, name, status, price_raw, features, url, city, first_seen, last_seen, last_status) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"id-{i}", f"Name {i}", status, "€1000", features_json, "https://x.com", "Eindhoven", first_seen, first_seen, status),
        )
    db._conn.commit()
    return db


class TestEnergyChartSort:
    def test_sorted_by_rank(self, db):
        data = db.chart_energy_dist()
        labels = [d["label"] for d in data]
        assert labels[0] == "A++"  # A++ → rank 1, first
        assert "A" in labels
        assert "B" in labels
        assert "C" in labels
        # A++ < A < B < C (ascending rank = better first)
        idx_a_plus_plus = labels.index("A++")
        idx_a = labels.index("A")
        idx_b = labels.index("B")
        idx_c = labels.index("C")
        assert idx_a_plus_plus < idx_a < idx_b < idx_c

    def test_no_listings(self, temp_db):
        data = temp_db.chart_energy_dist()
        assert data == []


class TestAreaChart:
    def test_buckets_correct(self, db):
        data = db.chart_area_dist()
        buckets_by_label = {d["label"]: d["count"] for d in data}
        assert buckets_by_label["20-30 m²"] == 1  # 25.0
        assert buckets_by_label["30-50 m²"] == 1  # 45.0
        assert buckets_by_label["50-80 m²"] == 1  # 65.0
        assert buckets_by_label[">80 m²"] == 1    # 90.0
        assert buckets_by_label["<20 m²"] == 0

    def test_all_buckets_present_even_if_zero(self, db):
        data = db.chart_area_dist()
        assert len(data) == 5  # 5 buckets always


class TestFloorChart:
    def test_buckets_correct(self, db):
        data = db.chart_floor_dist()
        buckets = {d["label"]: d["count"] for d in data}
        assert buckets["Ground"] == 1  # floor 0
        assert buckets["1-2"] == 1     # floor 1
        assert buckets["3-5"] == 1     # floor 3
        assert buckets["6+"] == 1      # floor 8


class TestBadFeaturesJSON:
    def test_bad_json_skipped_in_charts(self, temp_db):
        """损坏的 JSON 被 _count_feature_values / _bucketed_number_dist 跳过。"""
        temp_db._conn.execute(
            "INSERT INTO listings (id, name, status, price_raw, features, url, city, first_seen, last_seen, last_status) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("bad-1", "Bad", "Available", "€500", "NOT VALID JSON {{{", "https://x.com", "E", "2026-01-01", "2026-01-01", "Available"),
        )
        temp_db._conn.commit()

        # 不应抛异常
        data = temp_db.chart_energy_dist()
        assert isinstance(data, list)

        data = temp_db.chart_area_dist()
        assert isinstance(data, list)

        data = temp_db.chart_tenant_dist()
        assert isinstance(data, list)


class TestGeocodeCache:
    def test_cache_round_trip(self, temp_db):
        temp_db.cache_coords("Test Address, City", 51.44, 5.48)
        result = temp_db.get_cached_coords("Test Address, City")
        assert result is not None
        assert result[0] == pytest.approx(51.44)
        assert result[1] == pytest.approx(5.48)

    def test_cache_miss(self, temp_db):
        result = temp_db.get_cached_coords("Nonexistent")
        assert result is None

    def test_cache_overwrite(self, temp_db):
        temp_db.cache_coords("Addr", 1.0, 1.0)
        temp_db.cache_coords("Addr", 2.0, 2.0)
        result = temp_db.get_cached_coords("Addr")
        assert result == (2.0, 2.0)
