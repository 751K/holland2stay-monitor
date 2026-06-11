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
    from datetime import date

    from scrapers.xior import _to_listing

    # available_date 27 天后（在 60 天窗口内）→ 仍算可订
    listing = _to_listing(
        SAMPLE_UNIT, display="Maastricht Annadal",
        building_url="https://example.com", today=date(2026, 6, 4),
    )
    assert listing.status == "Available to book"
    assert listing.id == "xr_402419"
    assert listing.name == "Maastricht Annadal M1.30.53"
    assert listing.source == "xior"
    assert listing.price_raw == "€417–€580"
    assert listing.available_from == "2026-07-01"


# ── 可用日期窗口（_AVAILABLE_HORIZON_DAYS = 60）─────────────────────────

def test_window_far_future_notice_unrented_downgraded_to_occupied():
    """现住户一年后才搬走的 Notice Unrented → 不报可订（生产实测的假阳性）。"""
    from datetime import date

    from scrapers.xior import _to_listing

    unit = dict(SAMPLE_UNIT, availableDate="01/07/2027")  # 一年多以后
    listing = _to_listing(
        unit, display="Eindhoven Zernikestraat",
        building_url="https://example.com", today=date(2026, 6, 4),
    )
    assert listing.status == "Occupied"
    assert listing.available_from == "2027-07-01"  # 日期照常保留，仅状态降级


def test_window_boundary_60_days_still_available():
    from datetime import date

    from scrapers.xior import _to_listing

    unit = dict(SAMPLE_UNIT, availableDate="03/08/2026")  # 恰好 60 天
    listing = _to_listing(
        unit, display="X", building_url="https://e.com", today=date(2026, 6, 4),
    )
    assert listing.status == "Available to book"


def test_window_boundary_61_days_downgraded():
    from datetime import date

    from scrapers.xior import _to_listing

    unit = dict(SAMPLE_UNIT, availableDate="04/08/2026")  # 61 天
    listing = _to_listing(
        unit, display="X", building_url="https://e.com", today=date(2026, 6, 4),
    )
    assert listing.status == "Occupied"


def test_window_past_date_stays_available():
    """available_date 已过（单元应该已经空出）→ 不降级。"""
    from datetime import date

    from scrapers.xior import _to_listing

    unit = dict(SAMPLE_UNIT, availableDate="01/05/2026")  # 一个月前
    listing = _to_listing(
        unit, display="X", building_url="https://e.com", today=date(2026, 6, 4),
    )
    assert listing.status == "Available to book"


def test_window_missing_date_keeps_available():
    """日期缺失/不可解析时保守保留可订状态，避免漏报真房源。"""
    from datetime import date

    from scrapers.xior import _to_listing

    unit = dict(SAMPLE_UNIT, availableDate="")
    listing = _to_listing(
        unit, display="X", building_url="https://e.com", today=date(2026, 6, 4),
    )
    assert listing.status == "Available to book"
    assert listing.available_from is None


def test_window_also_applies_to_lottery_status():
    from datetime import date

    from scrapers.xior import _to_listing

    unit = dict(SAMPLE_UNIT, unitStatus="Vacant Unrented Not Ready", availableDate="01/07/2027")
    listing = _to_listing(
        unit, display="X", building_url="https://e.com", today=date(2026, 6, 4),
    )
    assert listing.status == "Occupied"


# ── floorplans.aspx 权威可订校验 ────────────────────────────────────────

# 真实 floorplans.aspx 结构的最小复刻：户型1 可订（applyButton + floorPlans id），
# 户型2 订不了（contactButton / Contact for Availability）。
FLOORPLANS_HTML = """
<div data-selenium-id ="FloorPlanAvailability" class="availability-count"> (Available) </div>
<table class="table"><tr><td>Deposit</td>
<button id="Comfy" data-selenium-id = "ApplyNow" class="applyButton btn btn-primary "
 onclick="location.href = 'termsandotheritems.aspx?myOlePropertyId=185589&floorPlans=1109741&UnitTypeId=29459'">Apply Now</button>
</td></tr></table>
<div data-selenium-id ="FloorPlanAvailability" class="availability-count"> (Contact for Availability) </div>
<table class="table"><tr><td>Deposit</td>
<button class="contactButton btn btn-primary " data-selenium-id = "ApplyNow"
 data-function='contactUsLink' onclick="showDialog('Contact Property','contactusdialog')">Contact</button>
</td></tr></table>
"""

APPLY_URL = (
    "https://zernikestraat-xiorstudenthousing.securerc.co.uk/onlineleasing/"
    "nlezerns-zernikestraat-1-9-eindhoven/oleapplication.aspx?stepname=RentalOptions"
    "&myLeaseCafeType=2&myOlePropertyId=185589&floorPlans=1109741&UnitTypeId=29459"
)


def test_parse_bookable_floorplan_ids_only_apply_button():
    from scrapers.xior import parse_bookable_floorplan_ids

    ids = parse_bookable_floorplan_ids(FLOORPLANS_HTML)
    assert ids == {1109741}  # contactButton 户型被排除


def test_parse_bookable_floorplan_ids_empty_when_none_available():
    from scrapers.xior import parse_bookable_floorplan_ids

    only_contact = FLOORPLANS_HTML.split("(Contact for Availability)")[0].replace(
        "(Available)", "(Contact for Availability)"
    ).replace("applyButton", "contactButton")
    assert parse_bookable_floorplan_ids(only_contact) == set()


def test_floorplans_url_derivation():
    from scrapers.xior import _floorplans_url

    url = _floorplans_url(APPLY_URL)
    assert url is not None
    assert url.endswith(
        "floorplans.aspx?stepname=Floorplan&myOlePropertyId=185589"
        "&propertyId=185589&IsFromBrochure=False&myLeaseCafeType=2"
        "&myStuApplicantType=Student"
    )
    assert "oleapplication.aspx" not in url


def test_floorplans_url_invalid_returns_none():
    from scrapers.xior import _floorplans_url

    assert _floorplans_url("") is None
    assert _floorplans_url("https://example.com/whatever") is None
    # 缺 myOlePropertyId
    assert _floorplans_url("https://x/onlineleasing/y/oleapplication.aspx?foo=1") is None


def test_floorplans_gate_downgrades_unbookable_floorplan():
    """单元在窗口内、但其 floorplan 不在权威可订集合 → 降级 Occupied（点进去会没）。"""
    from datetime import date

    from scrapers.xior import _to_listing

    unit = dict(SAMPLE_UNIT, floorplanId=999999, availableDate="01/07/2026")
    listing = _to_listing(
        unit, display="X", building_url="https://e.com",
        today=date(2026, 6, 4), bookable_floorplan_ids={1109741},
    )
    assert listing.status == "Occupied"


def test_floorplans_gate_keeps_bookable_floorplan():
    from datetime import date

    from scrapers.xior import _to_listing

    unit = dict(SAMPLE_UNIT, floorplanId=1109741, availableDate="01/07/2026")
    listing = _to_listing(
        unit, display="X", building_url="https://e.com",
        today=date(2026, 6, 4), bookable_floorplan_ids={1109741},
    )
    assert listing.status == "Available to book"


def test_floorplans_gate_fail_open_when_set_is_none():
    """bookable_floorplan_ids=None（floorplans.aspx 拿不到）→ 不 gate，信 WP feed。"""
    from datetime import date

    from scrapers.xior import _to_listing

    unit = dict(SAMPLE_UNIT, floorplanId=999999, availableDate="01/07/2026")
    listing = _to_listing(
        unit, display="X", building_url="https://e.com",
        today=date(2026, 6, 4), bookable_floorplan_ids=None,
    )
    assert listing.status == "Available to book"


def test_floorplans_gate_fail_open_when_floorplanid_unparseable():
    from datetime import date

    from scrapers.xior import _to_listing

    unit = dict(SAMPLE_UNIT, availableDate="01/07/2026")
    unit.pop("floorplanId", None)  # 没有 floorplanId
    listing = _to_listing(
        unit, display="X", building_url="https://e.com",
        today=date(2026, 6, 4), bookable_floorplan_ids={1109741},
    )
    assert listing.status == "Available to book"


def test_is_candidate_available():
    from datetime import date

    from scrapers.xior import _is_candidate_available

    today = date(2026, 6, 4)
    assert _is_candidate_available(dict(SAMPLE_UNIT, availableDate="01/07/2026"), today) is True
    assert _is_candidate_available(dict(SAMPLE_UNIT, availableDate="01/07/2027"), today) is False
    assert _is_candidate_available(dict(SAMPLE_UNIT, unitStatus="Occupied No Notice"), today) is False


def test_to_listing_maps_status_vacant_unrented():
    from scrapers.xior import _to_listing

    unit = dict(SAMPLE_UNIT, unitStatus="Vacant Unrented Not Ready")
    listing = _to_listing(unit, display="Maastricht Annadal", building_url="https://example.com")
    assert listing.status == "Available in lottery"


def test_to_listing_unknown_status_falls_back_to_occupied():
    """v1.7.9 安全加固：未知状态 fail-closed → Occupied（防误判为可预订）。"""
    from scrapers.xior import _to_listing

    unit = dict(SAMPLE_UNIT, unitStatus="Some New Status")
    listing = _to_listing(unit, display="Maastricht Annadal", building_url="https://example.com")
    assert listing.status == "Occupied"


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
