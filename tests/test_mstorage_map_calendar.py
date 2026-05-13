"""mstorage 地图+日历模块单元测试。"""

import pytest
from mstorage import Storage


@pytest.fixture
def store(tmp_path):
    s = Storage(tmp_path / "test.db")
    yield s
    s.close()


def _add(st, **kw):
    st._conn.execute(
        """INSERT OR REPLACE INTO listings
           (id, name, status, price_raw, available_from, features, url, city,
            first_seen, last_seen, notified, last_status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            kw.get("id", "x"), kw.get("name", "x"),
            kw.get("status", "Available to book"),
            kw.get("price_raw", "€700"),
            kw.get("available_from", kw.get("available_from", "")),
            kw.get("features", "[]"), kw.get("url", "https://t/x"),
            kw.get("city", "Eindhoven"),
            "2026-05-01T00:00:00", "2026-05-13T00:00:00", 0,
            kw.get("status", "Available to book"),
        ),
    )
    st._conn.commit()


class TestCalendar:
    def test_returns_only_with_available_from(self, store):
        _add(store, id="L1", available_from="2026-06-01")
        _add(store, id="L2", available_from="")  # empty → excluded
        _add(store, id="L3", available_from="2026-07-01")
        items = store.get_calendar_listings()
        assert len(items) == 2
        ids = {i["id"] for i in items}
        assert ids == {"L1", "L3"}

    def test_sorted_by_available_from(self, store):
        _add(store, id="L1", available_from="2026-08-01")
        _add(store, id="L2", available_from="2026-06-01")
        items = store.get_calendar_listings()
        assert items[0]["available_from"] == "2026-06-01"

    def test_extracts_building_from_features(self, store):
        _add(store, id="L1", available_from="2026-06-01",
             features='["Building: De Flat", "Area: 30 m²"]')
        items = store.get_calendar_listings()
        assert items[0]["building"] == "De Flat"

    def test_empty_db(self, store):
        assert store.get_calendar_listings() == []


class TestMap:
    def test_get_map_listings(self, store):
        _add(store, id="L1", name="Studio 1", city="Amsterdam")
        _add(store, id="L2", name="Studio 2", city="Utrecht")
        items = store.get_map_listings()
        assert len(items) == 2

    def test_address_includes_country(self, store):
        _add(store, id="L1", name="De Studio", city="Eindhoven")
        items = store.get_map_listings()
        assert "Netherlands" in items[0]["address"]

    def test_den_bosch_formal_name(self, store):
        _add(store, id="L1", name="Apartment", city="Den Bosch")
        items = store.get_map_listings()
        assert "'s-Hertogenbosch" in items[0]["address"]


class TestGeocodeCache:
    def test_cache_miss(self, store):
        assert store.get_cached_coords("Some Address") is None

    def test_cache_hit(self, store):
        store.cache_coords("Test Address", 51.44, 5.47)
        coords = store.get_cached_coords("Test Address")
        assert coords == (51.44, 5.47)

    def test_cache_overwrite(self, store):
        store.cache_coords("Addr", 1.0, 2.0)
        store.cache_coords("Addr", 3.0, 4.0)
        assert store.get_cached_coords("Addr") == (3.0, 4.0)


class TestResetAll:
    def test_clears_all_tables(self, store):
        _add(store, id="L1")
        store.add_web_notification(type="test", title="x")
        store.set_meta("k", "v")
        store.cache_coords("Addr", 1.0, 2.0)

        store.reset_all()

        assert store.count_all() == 0
        assert store.get_notifications() == []
        assert store.get_meta("k") == "—"
        assert store.get_cached_coords("Addr") is None
