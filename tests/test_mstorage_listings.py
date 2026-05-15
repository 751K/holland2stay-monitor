"""mstorage 房源查询单元测试 — get_all_listings / get_recent_changes / counts / filter helpers。"""

import pytest
from datetime import datetime, timezone, timedelta
from models import Listing
from mstorage import Storage


def _now_iso(**delta_kw) -> str:
    """当前 UTC 时间的 ISO 字符串，可传入 timedelta 参数偏移。"""
    dt = datetime.now(timezone.utc)
    if delta_kw:
        dt = dt + timedelta(**delta_kw)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


@pytest.fixture
def store(tmp_path):
    s = Storage(tmp_path / "test.db")
    yield s
    s.close()


def _add(st: Storage, id: str, **kw):
    """快速插入一条房源，绕过 diff。"""
    st.conn.execute(
        """INSERT OR REPLACE INTO listings
           (id, name, status, price_raw, available_from, features, url, city,
            first_seen, last_seen, notified, last_status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            id, kw.get("name", id), kw.get("status", "Available to book"),
            kw.get("price_raw", "€700"), kw.get("available_from", ""),
            kw.get("features", "[]"), kw.get("url", f"https://t/{id}"),
            kw.get("city", kw.get("city", "Eindhoven")),
            kw.get("first_seen", _now_iso(hours=-1)),
            kw.get("last_seen", _now_iso(minutes=-30)),
            kw.get("notified", 0), kw.get("last_status", kw.get("status", "Available to book")),
        ),
    )
    st.conn.commit()


class TestGetAllListings:
    def test_returns_all(self, store):
        _add(store, "L1")
        _add(store, "L2")
        items = store.get_all_listings()
        assert len(items) == 2

    def test_filter_by_status(self, store):
        _add(store, "L1", status="Available to book")
        _add(store, "L2", status="In lottery")
        items = store.get_all_listings(status="In lottery")
        assert len(items) == 1
        assert items[0]["id"] == "L2"

    def test_filter_by_city(self, store):
        _add(store, "L1", city="Amsterdam")
        _add(store, "L2", city="Utrecht")
        items = store.get_all_listings(city="Amsterdam")
        assert len(items) == 1

    def test_search_by_name(self, store):
        _add(store, "L1", name="Sunny Studio")
        _add(store, "L2", name="Dark Basement")
        items = store.get_all_listings(search="sunny")
        assert len(items) == 1
        assert items[0]["name"] == "Sunny Studio"

    def test_respects_limit(self, store):
        for i in range(10):
            _add(store, f"L{i}")
        items = store.get_all_listings(limit=3)
        assert len(items) == 3


class TestGetRecentChanges:
    def test_returns_changes(self, store):
        _add(store, "L1", name="Test")
        now = _now_iso()
        store.conn.execute(
            """INSERT INTO status_changes (listing_id, old_status, new_status, changed_at)
               VALUES ('L1', 'In lottery', 'Available to book', ?)""",
            (now,),
        )
        store.conn.commit()
        changes = store.get_recent_changes(hours=24)
        assert len(changes) == 1
        assert changes[0]["name"] == "Test"
        assert changes[0]["old_status"] == "In lottery"

    def test_filter_by_city(self, store):
        _add(store, "L1", city="Amsterdam")
        _add(store, "L2", city="Utrecht")
        now = _now_iso()
        for lid in ("L1", "L2"):
            store.conn.execute(
                """INSERT INTO status_changes (listing_id, old_status, new_status, changed_at)
                   VALUES (?, 'old', 'new', ?)""", (lid, now),
            )
        store.conn.commit()
        assert len(store.get_recent_changes(hours=24, city="Amsterdam")) == 1
        assert len(store.get_recent_changes(hours=24)) == 2

    def test_outside_window_excluded(self, store):
        _add(store, "L1")
        store.conn.execute(
            """INSERT INTO status_changes (listing_id, old_status, new_status, changed_at)
               VALUES ('L1', 'old', 'new', '2020-01-01T00:00:00')"""
        )
        store.conn.commit()
        assert store.get_recent_changes(hours=24) == []


class TestCounts:
    def test_count_new_since(self, store):
        now = _now_iso()
        _add(store, "L1", first_seen=now)
        _add(store, "L2", first_seen="2020-01-01T00:00:00")
        assert store.count_new_since(hours=24) == 1

    def test_count_changes_since(self, store):
        _add(store, "L1")
        now = _now_iso()
        store.conn.execute(
            """INSERT INTO status_changes (listing_id, old_status, new_status, changed_at)
               VALUES ('L1', 'old', 'new', ?)""",
            (now,),
        )
        store.conn.commit()
        assert store.count_changes_since(hours=24) == 1

    def test_count_all(self, store):
        _add(store, "L1", city="A")
        _add(store, "L2", city="A")
        _add(store, "L3", city="B")
        assert store.count_all() == 3
        assert store.count_all(city="A") == 2


class TestFilterHelpers:
    def test_get_distinct_cities(self, store):
        _add(store, "L1", city="Amsterdam")
        _add(store, "L2", city="Utrecht")
        _add(store, "L3", city="Amsterdam")
        cities = store.get_distinct_cities()
        assert cities == ["Amsterdam", "Utrecht"]

    def test_get_distinct_statuses(self, store):
        _add(store, "L1", status="Available")
        _add(store, "L2", status="Lottery")
        assert set(store.get_distinct_statuses()) == {"Available", "Lottery"}

    def test_get_feature_values(self, store):
        _add(store, "L1", features='["Tenant: Student", "Tenant: PhD"]')  # only first counts
        _add(store, "L2", features='["Tenant: Student"]')
        vals = store.get_feature_values("Tenant")
        labels = {v for v in vals}
        assert "Student" in labels

    def test_get_feature_values_with_city_filter(self, store):
        _add(store, "L1", features='["Tenant: Student"]', city="A")
        _add(store, "L2", features='["Tenant: Professional"]', city="B")
        vals = store.get_feature_values("Tenant", cities=["A"])
        assert vals == ["Student"]

    def test_get_listing(self, store):
        _add(store, "xyz")
        item = store.get_listing("xyz")
        assert item is not None
        assert item["id"] == "xyz"

    def test_get_listing_missing(self, store):
        assert store.get_listing("nonexistent") is None
