"""monitor Phase 3 stale listing 收敛测试。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from models import Listing
from monitor import _mark_stale_listings_for_complete_cities


def _listing(
    listing_id: str,
    *,
    city: str,
    status: str = "Available to book",
    source: str = "holland2stay",
) -> Listing:
    return Listing(
        id=listing_id,
        name=f"Listing {listing_id}",
        status=status,
        price_raw="€1000",
        available_from="2030-01-01",
        features=[],
        url=f"https://example.test/{listing_id}",
        city=city,
        source=source,
    )


def _set_last_seen(temp_db, listing_id: str, days_ago: int) -> None:
    last_seen = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    with temp_db.conn:
        temp_db.conn.execute(
            "UPDATE listings SET last_seen=? WHERE id=?",
            (last_seen, listing_id),
        )


class TestMonitorStaleSweep:
    def test_marks_only_complete_cities(self, temp_db):
        temp_db.diff([
            _listing("e", city="Eindhoven"),
            _listing("a", city="Amsterdam"),
        ])
        _set_last_seen(temp_db, "e", 8)
        _set_last_seen(temp_db, "a", 8)

        updated = _mark_stale_listings_for_complete_cities(
            temp_db,
            {"Eindhoven": True, "Amsterdam": False},
            days=7,
        )

        assert updated == 1
        assert temp_db.get_listing("e")["status"] == "Occupied"
        assert temp_db.get_listing("a")["status"] == "Available to book"

    def test_no_complete_city_is_noop(self, temp_db):
        temp_db.diff([_listing("e", city="Eindhoven")])
        _set_last_seen(temp_db, "e", 8)

        updated = _mark_stale_listings_for_complete_cities(
            temp_db,
            {"Eindhoven": False},
            days=7,
        )

        assert updated == 0
        assert temp_db.get_listing("e")["status"] == "Available to book"

    def test_empty_completeness_is_noop(self, temp_db):
        temp_db.diff([_listing("e", city="Eindhoven")])
        _set_last_seen(temp_db, "e", 8)

        updated = _mark_stale_listings_for_complete_cities(temp_db, {}, days=7)

        assert updated == 0
        assert temp_db.get_listing("e")["status"] == "Available to book"

    def test_lottery_window_passed_to_storage(self, temp_db):
        temp_db.diff([
            _listing("book", city="Eindhoven", status="Available to book"),
            _listing("lottery", city="Eindhoven", status="Available in lottery"),
        ])
        _set_last_seen(temp_db, "book", 3)
        _set_last_seen(temp_db, "lottery", 3)

        updated = _mark_stale_listings_for_complete_cities(
            temp_db,
            {"Eindhoven": True},
            days=7,
            lottery_days=2,
        )

        assert updated == 1
        assert temp_db.get_listing("book")["status"] == "Available to book"
        assert temp_db.get_listing("lottery")["status"] == "Occupied"

    def test_source_prefixed_completeness_limits_stale_to_source_city(self, temp_db):
        temp_db.diff([
            _listing("h2s", city="Amsterdam Diemen", source="holland2stay"),
            _listing("od", city="Amsterdam Diemen", source="ourdomain"),
        ])
        _set_last_seen(temp_db, "h2s", 8)
        _set_last_seen(temp_db, "od", 8)

        updated = _mark_stale_listings_for_complete_cities(
            temp_db,
            {"ourdomain:Amsterdam Diemen": True},
            days=7,
        )

        assert updated == 1
        assert temp_db.get_listing("h2s")["status"] == "Available to book"
        assert temp_db.get_listing("od")["status"] == "Occupied"
