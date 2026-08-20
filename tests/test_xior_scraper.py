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
    """``Vacant Unrented Not Ready`` 也是可订，不是「抽签」。

    **Xior 没有抽签机制**——"lottery" 是 Holland2Stay 专有概念（H2S 的
    availability filter id=336 摇号池）。这里曾经映射成 ``Available in lottery``，
    实测那批单元和 ``Notice Unrented`` 一样带 ``applyOnlineURL``、
    ``availableDate`` 分布重叠、同样要过闸②，用户看到的却是一个不存在的摇号。

    错标还会连累 stale 收敛：lottery 用的是 2 天阈值而非 7 天，这些单元会以
    3.5 倍的速度被推测成 Occupied。
    """
    from scrapers.xior import _to_listing

    unit = dict(SAMPLE_UNIT, unitStatus="Vacant Unrented Not Ready")
    listing = _to_listing(unit, display="Maastricht Annadal", building_url="https://example.com")
    assert listing.status == "Available to book"


def test_xior_never_produces_lottery_status():
    """回归：Xior 平台不该产出任何 lottery 状态。"""
    from scrapers.xior import _AVAILABLE_STATUSES, _STATUS_MAP

    assert "Available in lottery" not in _STATUS_MAP.values()
    assert "Available in lottery" not in _AVAILABLE_STATUSES


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


class FakeFetcher:
    """BrowserFetcher 替身：按序返回 envelope，或抛出预置异常。

    Xior 的 AJAX 端点已被 Cloudflare 挡住，传输层因此换成了浏览器；
    CF 相关的失败由 BrowserFetcher 抛出，_post_ajax 只处理业务语义和限流。
    """

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def fetch_form(self, path, data, *, timeout_ms=30_000, headers=None):
        self.calls.append((path, dict(data)))
        outcome = (
            self.outcomes.pop(0) if self.outcomes
            else {"success": True, "data": {"units": [], "total": 0}}
        )
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_post_ajax_success():
    """成功响应被正确解包。"""
    from scrapers.xior import _post_ajax

    fetcher = FakeFetcher(
        {"success": True, "data": {"units": [SAMPLE_UNIT], "total": 1}}
    )
    result = _post_ajax(
        fetcher, property_page_id=1114, room_type_id=33934, semester_id=3281
    )
    assert result is not None
    assert len(result["units"]) == 1
    # 走同源路径而不是绝对 URL——origin 由 profile 的 challenge_url 决定
    assert fetcher.calls[0][0] == "/wp-admin/admin-ajax.php"
    assert fetcher.calls[0][1]["action"] == "yardi_room_availability"


def test_post_ajax_business_failure_returns_none():
    """success=false 是业务失败，不是屏蔽 → None（本轮 incomplete）。"""
    from scrapers.xior import _post_ajax

    fetcher = FakeFetcher(
        {"success": False, "data": {"message": "too many requests"}}
    )
    assert _post_ajax(
        fetcher, property_page_id=1114, room_type_id=33934, semester_id=3281
    ) is None


def test_post_ajax_treats_204_as_genuine_zero_availability():
    """204 是 Xior 表达「当前无可用单元」的方式，不是故障。

    用官方前端验证过：在站点自己的 modal 里选房型、走完带 Turnstile 的完整
    流程，前端收到的同样是 ``success=true`` + ``units=[]`` + errorCode 204。
    把它当失败会让每一轮零可用都被标成 incomplete，stale 收敛永远不执行。
    """
    from scrapers.xior import _post_ajax

    fetcher = FakeFetcher({
        "success": True,
        "data": {
            "units": [],
            "total": 0,
            "availability_params": {"academicTermId": 3281},
            "availability_response": {"errorCode": 204, "errorMessage": "Unknown error"},
        },
    })

    result = _post_ajax(
        fetcher, property_page_id=1126, room_type_id=33944, semester_id=3281
    )
    assert result is not None
    assert result["units"] == []


def test_post_ajax_treats_200_as_success():
    """errorCode 200 是成功，不是故障。

    回归：曾用「非 204 即故障」当判据，结果 Naritaweg 返回的 200 被当成错误，
    整晚每一轮、每个房型都被误标为 incomplete。该字段装的是
    HTTP 风格状态码，2xx 都是成功。
    """
    from scrapers.xior import _post_ajax

    fetcher = FakeFetcher({
        "success": True,
        "data": {
            "units": [SAMPLE_UNIT],
            "total": 1,
            "availability_response": {"errorCode": 200, "errorMessage": ""},
        },
    })

    result = _post_ajax(
        fetcher, property_page_id=499, room_type_id=29891, semester_id=3281
    )
    assert result is not None
    assert len(result["units"]) == 1


def test_post_ajax_detects_real_upstream_error():
    """204 之外的 errorCode 才是真故障 → None（本轮 incomplete）。

    WP 层的 success=true 只说明请求到了它那里；上游 Yardi 的结果在
    availability_response 里，不查就会把上游挂掉读成「没有房源」。
    """
    from scrapers.xior import _post_ajax

    fetcher = FakeFetcher({
        "success": True,
        "data": {
            "units": [],
            "total": 0,
            "availability_response": {"errorCode": 500, "errorMessage": "Server error"},
        },
    })

    assert _post_ajax(
        fetcher, property_page_id=1126, room_type_id=33944, semester_id=3281
    ) is None


def test_post_ajax_accepts_response_without_upstream_error():
    from scrapers.xior import _post_ajax

    fetcher = FakeFetcher({
        "success": True,
        "data": {"units": [SAMPLE_UNIT], "total": 1, "availability_response": {}},
    })

    result = _post_ajax(
        fetcher, property_page_id=1126, room_type_id=33944, semester_id=3281
    )
    assert result is not None
    assert len(result["units"]) == 1


def test_post_ajax_propagates_blocked_without_retrying(monkeypatch):
    """CF 屏蔽必须原样上抛给 source 级熔断，且不该继续重试。

    回归：以前 403 被当成可重试的失败，最终返回 None 只把本轮标成
    incomplete，熔断不触发，于是每轮重来一遍、每栋楼白等 90s 退避，
    而 Cloudflare 挡着永远不可能成功。
    """
    import scrapers.xior as xior
    from scrapers.base import BlockedError

    monkeypatch.setattr(xior.time, "sleep", lambda _: None)
    fetcher = FakeFetcher(BlockedError("CF 持续 403"))

    with pytest.raises(BlockedError):
        xior._post_ajax(
            fetcher, property_page_id=1114, room_type_id=33934, semester_id=3281
        )
    assert len(fetcher.calls) == 1, "被屏蔽后不该继续重试"


def test_post_ajax_propagates_maintenance(monkeypatch):
    import scrapers.xior as xior
    from scrapers.base import UpstreamMaintenanceError

    monkeypatch.setattr(xior.time, "sleep", lambda _: None)
    fetcher = FakeFetcher(UpstreamMaintenanceError("维护中"))

    with pytest.raises(UpstreamMaintenanceError):
        xior._post_ajax(
            fetcher, property_page_id=1114, room_type_id=33934, semester_id=3281
        )
    assert len(fetcher.calls) == 1


def test_post_ajax_retries_rate_limit_then_succeeds(monkeypatch):
    """429 是临时的，退避后重试。"""
    import scrapers.xior as xior
    from scrapers.base import RateLimitError

    monkeypatch.setattr(xior.time, "sleep", lambda _: None)
    fetcher = FakeFetcher(
        RateLimitError("429"),
        {"success": True, "data": {"units": [SAMPLE_UNIT], "total": 1}},
    )

    result = xior._post_ajax(
        fetcher, property_page_id=1114, room_type_id=33934, semester_id=3281
    )
    assert result is not None
    assert len(fetcher.calls) == 2


def test_post_ajax_network_errors_exhaust_to_none(monkeypatch):
    """网络类错误重试用尽 → None（本轮 incomplete），不升级成屏蔽。"""
    import scrapers.xior as xior
    from scrapers.base import ScrapeNetworkError

    monkeypatch.setattr(xior.time, "sleep", lambda _: None)
    fetcher = FakeFetcher(*[
        ScrapeNetworkError("boom")
        for _ in range(len(xior.RATE_LIMIT_BACKOFF) + 1)
    ])

    assert xior._post_ajax(
        fetcher, property_page_id=1114, room_type_id=33934, semester_id=3281
    ) is None


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


# ── 权威校验必须和浏览器走同一条出口 ────────────────────────────────────

class TestFloorplanCheckSharesTheBrowserExit:
    """floorplans.aspx 的权威校验要走**浏览器实际在用的那条代理**。

    原实现在 ``scrape()`` 里另调一次 ``get_proxy_url(self.source)``——注意
    **没有 rotating=True**，于是它拿到的是一条永不轮换的 sticky session，
    和浏览器（``XIOR_PROFILE.rotating_proxy=True``）根本是两个出口 IP。

    两个后果：

    1. **不一致。** WP feed 和「这户型到底还能不能订」这两份数据来自不同 IP，
       而这条校验恰恰只在有候选可订单元时才发——也就是决定一条房源真假的
       那一刻。
    2. **多烧一份限流额度，而且是烧在一个永不轮换的 IP 上。** Xior 整套抗
       限流策略就是「换浏览器换 IP 把累积量摊开」（见模块头 Rate limiting），
       唯独这条最关键的请求被钉死在固定 IP 上。它打的还是 RentCafe
       （``*.securerc.co.uk``），和 OurDomain 同一个平台，而 OurDomain 那边
       用的是 ``rotating=True``。

    ``BrowserFetcher`` 早就把实际生效的代理记在 ``_proxy_url`` 上了，就是为了
    「诊断必须探这个浏览器实际在用的那条线路」——同一个理由适用于这里。
    """

    def test_browser_fetcher_exposes_its_proxy(self):
        from browser_fetcher import BrowserFetcher

        f = BrowserFetcher.__new__(BrowserFetcher)
        f._proxy_url = "http://u:p@gw.example:8080"
        assert f.proxy_url == "http://u:p@gw.example:8080"

    def test_verify_uses_the_fetchers_proxy_not_a_second_session(self, monkeypatch):
        import scrapers.xior as x

        seen: dict = {}

        class _Sess:
            def __init__(self, **kw):
                seen.update(kw)

            def __enter__(self): return self
            def __exit__(self, *a): return False

        monkeypatch.setattr(x.req, "Session", _Sess)
        monkeypatch.setattr(x, "_fetch_bookable_floorplan_ids", lambda s, u: {1})
        # 诱饵：本次修复把这个符号从模块里删掉了。若哪天有人把它加回来并在
        # 校验路径上用，桩会让断言立刻看见走错了出口。raising=False 是因为
        # 现在它本就不该存在。
        monkeypatch.setattr(
            x, "get_proxy_url", lambda *a, **k: "http://WRONG:1", raising=False,
        )

        class _Fetcher:
            proxy_url = "http://RIGHT:2"

        s = x.XiorScraper()
        unit = {
            "unitStatus": "Notice Unrented",
            "availableDate": "01/07/2026",
            "applyOnlineURL": (
                "https://x.securerc.co.uk/onlineleasing/a/oleapplication.aspx"
                "?myOlePropertyId=185589&myLeaseCafeType=2"
            ),
        }
        monkeypatch.setattr(x, "_is_candidate_available", lambda u, t: True)

        from datetime import date
        s._verify_bookable_floorplans([unit], date(2026, 1, 1), _Fetcher(), "B")

        assert seen.get("proxies") == {
            "https": "http://RIGHT:2", "http": "http://RIGHT:2",
        }, f"权威校验没走浏览器那条代理: {seen.get('proxies')!r}"

    def test_no_proxy_means_no_proxies_dict(self, monkeypatch):
        """浏览器降级成直连（代理全在冷却）时，校验也该直连。"""
        import scrapers.xior as x
        from datetime import date

        seen: dict = {}

        class _Sess:
            def __init__(self, **kw):
                seen.update(kw)

            def __enter__(self): return self
            def __exit__(self, *a): return False

        monkeypatch.setattr(x.req, "Session", _Sess)
        monkeypatch.setattr(x, "_fetch_bookable_floorplan_ids", lambda s, u: set())
        monkeypatch.setattr(x, "_is_candidate_available", lambda u, t: True)

        class _Fetcher:
            proxy_url = ""

        s = x.XiorScraper()
        s._verify_bookable_floorplans(
            [{"applyOnlineURL":
              "https://x.securerc.co.uk/onlineleasing/a/oleapplication.aspx"
              "?myOlePropertyId=1&myLeaseCafeType=2"}],
            date(2026, 1, 1), _Fetcher(), "B",
        )
        assert seen.get("proxies") == {}

    def test_scrape_no_longer_opens_its_own_sticky_session(self):
        """回归守卫：``scrape()`` 里不该再出现非 rotating 的 get_proxy_url。"""
        import inspect

        import scrapers.xior as x

        src = inspect.getsource(x.XiorScraper.scrape)
        assert "get_proxy_url" not in src, (
            "scrape() 又自己开了一条 sticky 代理会话——它和浏览器不是同一个出口 IP"
        )
