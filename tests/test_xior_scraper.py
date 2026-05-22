"""Unit tests for XiorScraper — JSON parsing, Listing mapping, date normalisation."""
from __future__ import annotations

import pytest

# Fixtures — minimal unit JSON used by the scraper's _to_listing and helpers


SAMPLE_UNIT = {
    "apartmentId": 402419,
    "apartmentName": "M1.30.53",
    "floorplanId": 1111471,
    "floorplanName": "Essential (Second - Fifth floor)",
    "beds": 1,
    "baths": 0,
    "sqm": 19,
    "minimumRent": 417,
    "maximumRent": 580,
    "deposit": 0,
    "availableDate": "01/07/2026",
    "unitStatus": "Notice Unrented",
    "applyOnlineURL": "https://brouwersweg-xiorstudenthousing.securerc.co.uk/onlineleasing/",
}


def test_to_listing_maps_status_notice_unrented():
    from scrapers.xior import _to_listing

    listing = _to_listing(SAMPLE_UNIT, display="Maastricht Annadal", building_url="https://example.com")
    assert listing.status == "Available to book"
    assert listing.id == "xr_402419"
    assert listing.name == "Maastricht Annadal M1.30.53"
    assert listing.source == "xior"
    assert listing.price_raw == "€417–€580"
    assert listing.available_from == "2026-07-01"


def test_to_listing_maps_status_vacant_unrented():
    from scrapers.xior import _to_listing

    unit = dict(SAMPLE_UNIT, unitStatus="Vacant Unrented Not Ready")
    listing = _to_listing(unit, display="Maastricht Annadal", building_url="https://example.com")
    assert listing.status == "Available in lottery"


def test_to_listing_unknown_status_falls_back_to_available():
    from scrapers.xior import _to_listing

    unit = dict(SAMPLE_UNIT, unitStatus="Some New Status")
    listing = _to_listing(unit, display="Maastricht Annadal", building_url="https://example.com")
    assert listing.status == "Available to book"


def test_to_listing_single_rent_when_min_equals_max():
    from scrapers.xior import _to_listing

    unit = dict(SAMPLE_UNIT, minimumRent=500, maximumRent=500)
    listing = _to_listing(unit, display="Maastricht Annadal", building_url="https://example.com")
    assert listing.price_raw == "€500"


def test_to_listing_features_include_unit_sqm_floorplan():
    from scrapers.xior import _to_listing

    listing = _to_listing(SAMPLE_UNIT, display="Maastricht Annadal", building_url="https://example.com")
    features_str = " ".join(listing.features)
    assert "M1.30.53" in features_str
    assert "19 m²" in features_str
    assert "Essential" in features_str


def test_to_listing_zero_deposit_shown():
    from scrapers.xior import _to_listing

    listing = _to_listing(SAMPLE_UNIT, display="Maastricht Annadal", building_url="https://example.com")
    assert "Deposit: €0" in listing.features


def test_to_listing_positive_deposit_shown():
    from scrapers.xior import _to_listing

    unit = dict(SAMPLE_UNIT, deposit=350)
    listing = _to_listing(unit, display="Maastricht Annadal", building_url="https://example.com")
    assert "Deposit: €350" in listing.features


def test_to_listing_missing_deposit_defaults_to_zero():
    from scrapers.xior import _to_listing

    unit = {k: v for k, v in SAMPLE_UNIT.items() if k != "deposit"}
    listing = _to_listing(unit, display="Maastricht Annadal", building_url="https://example.com")
    # missing deposit → defaults to 0 → shown as €0
    assert "Deposit: €0" in listing.features


def test_to_listing_uses_url_from_unit():
    from scrapers.xior import _to_listing

    listing = _to_listing(SAMPLE_UNIT, display="Maastricht Annadal", building_url="https://fallback.example.com")
    assert "brouwersweg-xiorstudenthousing" in listing.url


def test_to_listing_falls_back_to_building_url():
    from scrapers.xior import _to_listing

    unit = dict(SAMPLE_UNIT)
    del unit["applyOnlineURL"]
    listing = _to_listing(unit, display="Maastricht Annadal", building_url="https://example.com")
    assert listing.url == "https://example.com"


def test_normalise_date_standard():
    from scrapers.xior import _normalise_date

    assert _normalise_date("01/07/2026") == "2026-07-01"
    assert _normalise_date("31/12/2025") == "2025-12-31"
    assert _normalise_date("5/3/2026") == "2026-03-05"


def test_normalise_date_invalid():
    from scrapers.xior import _normalise_date

    assert _normalise_date("") is None
    assert _normalise_date("not-a-date") is None
    assert _normalise_date("2026-07-01") is None  # ISO format not accepted


# ── Integration: _post_ajax with mock ────────────────────────────────


class FakeResponse:
    def __init__(self, status_code, json_data):
        self.status_code = status_code
        self._json = json_data
        self.ok = 200 <= status_code < 300
        self.text = str(json_data)

    def json(self):
        return self._json


class FakeSession:
    """Records calls; returns canned responses."""
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, data, timeout):
        self.calls.append((url, data))
        if self.responses:
            return self.responses.pop(0)
        return FakeResponse(200, {"success": True, "data": {"units": [], "total": 0}})


def test_post_ajax_success(monkeypatch):
    """Integration: _post_ajax parses a successful response."""
    from scrapers.xior import _post_ajax

    session = FakeSession(
        FakeResponse(200, {"success": True, "data": {"units": [SAMPLE_UNIT], "total": 1}})
    )
    result = _post_ajax(session, property_page_id=1114, room_type_id=33934, semester_id=3281)
    assert result is not None
    assert len(result["units"]) == 1


def test_post_ajax_business_failure_returns_none(monkeypatch):
    from scrapers.xior import _post_ajax

    session = FakeSession(
        FakeResponse(200, {"success": False, "data": {"message": "too many requests"}})
    )
    result = _post_ajax(session, property_page_id=1114, room_type_id=33934, semester_id=3281)
    assert result is None


def test_post_ajax_business_failure_returns_none():
    """success=false in envelope → None."""
    from scrapers.xior import _post_ajax

    session = FakeSession(
        FakeResponse(200, {"success": False, "data": {"message": "too many requests"}})
    )
    result = _post_ajax(session, property_page_id=1114, room_type_id=33934, semester_id=3281)
    assert result is None


def test_post_ajax_retry_exhausted_returns_none(monkeypatch):
    """Three consecutive non-200 (non-429) responses → None after retries."""
    from scrapers.xior import _post_ajax

    monkeypatch.setattr("scrapers.xior.time.sleep", lambda _: None)
    session = FakeSession(
        FakeResponse(500, "err"),
        FakeResponse(500, "err"),
        FakeResponse(500, "err"),
    )
    result = _post_ajax(session, property_page_id=1114, room_type_id=33934, semester_id=3281)
    assert result is None


# ── Scraper registration ─────────────────────────────────────────────


def test_xior_registered():
    from scrapers import SCRAPER_REGISTRY, get_scraper

    assert "xior" in SCRAPER_REGISTRY
    scraper = get_scraper("xior")
    assert scraper is not None
    assert scraper.source == "xior"


def test_xior_building_lookup():
    from scrapers.xior import XiorScraper

    scraper = XiorScraper()
    bldg = scraper._building_for_task(
        type("Task", (), {"city_key": "p0196111", "city_display": "X", "extra": {}})()
    )
    assert bldg["property_page_id"] == 1114
    assert bldg["display"] == "Maastricht Annadal"
