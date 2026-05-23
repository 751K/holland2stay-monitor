from __future__ import annotations

import pytest

from scrapers.base import BlockedError, ScrapeTask
from scrapers.ourdomain import (
    OurDomainScraper,
    _cell_text,
    _extract_floorplan_ids,
    _extract_floorplan_names,
    _extract_unit,
    _extract_units,
    _infer_occupancy,
    parse_ourdomain_floor,
)


FLOORPLANS_HTML = """
<!-- Anchor 形态：subPointerId=NNN + 同一 anchor 的 title="..."
     是真实 server-side HTML 里 FP 信息的稳定来源（dropdown checkbox
     是 JS hydrated 的，curl_cffi 看不到）。 -->
<a onclick="showDialog('Floor Plan Superior Studio 1-person max.',
    '...subPointerId=1107060&myPropertyId=184283...');"
    title="Superior Studio 1-person max. | Furnished | Short Stay | all-in rent*">Short Stay</a>
<a onclick="showDialog('Floor Plan Executive Studio',
    '...subPointerId=1106316&myPropertyId=184283...');"
    title="Executive Studio | Furnished | Contract 1-5 years">1-5y Contract</a>
<a onclick="showDialog('Floor Plan Executive Studio',
    '...subPointerId=1107060&myPropertyId=184283...');"
    title="Superior Studio 1-person max. | Furnished | Short Stay | all-in rent*">Duplicate</a>
"""


UNITS_HTML_1 = """
<table>
  <tr id="unitrow_307195" data-selenium-id="urow1">
    <th data-selenium-id="Apt1" id="307195">#6045</th>
    <td data-selenium-id="SqFt1">22</td>
    <td data-selenium-id="Rent1">€ 1.587</td>
    <td data-selenium-id="Deposit1">€ 2.622</td>
    <td data-selenium-id="Amenity1">
      <label>Ground Floor</label>
      <label>Courtyard View</label>
    </td>
    <td data-selenium-id="AvailDate1">
      <span class="text-success">Available</span>
    </td>
    <td data-selenium-id="Action1">
      <input value="Book now" onclick="ApplyNowClick('307195','1107060','184283','6-6-2026')" />
    </td>
  </tr>
  <tr id="unitrow_307302" data-selenium-id="urow2">
    <th data-selenium-id="Apt2" id="307302">#6222</th>
    <td data-selenium-id="SqFt2">23</td>
    <td data-selenium-id="Rent2">€ 1.563</td>
    <td data-selenium-id="Deposit2">€ 0</td>
    <td data-selenium-id="Amenity2"><label>Floor 1-4</label></td>
    <td data-selenium-id="AvailDate2"><span class="text-warning">Wait List</span></td>
    <td data-selenium-id="Action2"></td>
  </tr>
</table>
"""


UNITS_HTML_2 = """
<table>
  <tr id="unitrow_307195" data-selenium-id="urow1">
    <th data-selenium-id="Apt1" id="307195">#6045</th>
    <td data-selenium-id="SqFt1">22</td>
    <td data-selenium-id="Rent1">€ 1.587</td>
    <td data-selenium-id="Deposit1">€ 2.622</td>
    <td data-selenium-id="Amenity1"><label>Ground Floor</label></td>
    <td data-selenium-id="AvailDate1"><span class="text-success">Available</span></td>
    <td data-selenium-id="Action1"></td>
  </tr>
</table>
"""


class FakeResponse:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text
        self.ok = 200 <= status_code < 400

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    instances: list["FakeSession"] = []

    def __init__(self, *args, **kwargs):
        self.calls: list[str] = []
        self.impersonate = kwargs.get("impersonate")
        type(self).instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url: str, timeout: int = 30, **kwargs):
        self.calls.append(url)
        if "floorplans.aspx" in url:
            return FakeResponse(200, FLOORPLANS_HTML)
        if "floorPlans=1107060" in url:
            return FakeResponse(200, UNITS_HTML_1)
        if "floorPlans=1106316" in url:
            return FakeResponse(200, UNITS_HTML_2)
        return FakeResponse(404, "not found")


class BlockedSession(FakeSession):
    def get(self, url: str, timeout: int = 30, **kwargs):
        return FakeResponse(403, "<!DOCTYPE html><html>cloudflare challenge-platform</html>")


class FirstBlockedThenOkSession(FakeSession):
    instance_count = 0

    def __init__(self, *args, **kwargs):
        type(self).instance_count += 1
        self.blocked = type(self).instance_count == 1
        super().__init__(*args, **kwargs)

    def get(self, url: str, timeout: int = 30, **kwargs):
        if self.blocked and "floorplans.aspx" in url:
            return FakeResponse(403, "<!DOCTYPE html><html>cloudflare challenge-platform</html>")
        return super().get(url, timeout=timeout, **kwargs)


def test_extract_floorplan_ids_deduplicates_in_order():
    assert _extract_floorplan_ids(FLOORPLANS_HTML) == ["1107060", "1106316"]


def test_extract_floorplan_names_from_anchor_title():
    """从 anchor 的 `title="..."` 属性反解 FP 名（server-side 渲染稳定来源）。"""
    names = _extract_floorplan_names(FLOORPLANS_HTML)
    assert set(names) == {"1107060", "1106316"}
    assert names["1107060"].startswith("Superior Studio 1-person max")
    assert names["1106316"].startswith("Executive Studio")


@pytest.mark.parametrize("sqft,expected", [
    # sqft 主路径：荷兰租赁市场常规阈值
    ("22", "One"),                                  # 22 m² 单人 studio
    ("28", "One"),                                  # 28 m² 边缘单人
    ("30", "Two (only couples)"),                   # 30 m² 起 → 双人
    ("38", "Two (only couples)"),                   # 1-BR 公寓典型
    ("59", "Two (only couples)"),                   # H2S Kastanjelaan Loft 同档
    ("60", "Family (parents with children)"),       # 60 m² 起 → 家庭
    ("85", "Family (parents with children)"),       # 大公寓
    ("", None),                                     # 空 sqft → 走兜底
])
def test_infer_occupancy_from_sqft(sqft, expected):
    assert _infer_occupancy(sqft=sqft) == expected


@pytest.mark.parametrize("fp_names,expected", [
    # sqft 缺失时的兜底路径：仍保留 FP 名字匹配逻辑
    (["Executive Studio | Furnished | Contract 1-5 years"], "One"),
    (["Superior Studio 1-person max. | Short Stay"], "One"),
    (["Superior Plus Studio | Furnished | Short Stay"], "Two (only couples)"),
    (["1-Bedroom Apartment | Furnished | Contract 1-5 years"], "Two (only couples)"),
    (["2-Bedroom Apartment"], "Family (parents with children)"),
    # 都没线索 → None
    ([], None),
    (["Some random floor plan name"], None),
])
def test_infer_occupancy_fp_fallback(fp_names, expected):
    """sqft=None 时回退到 FP 名字匹配。"""
    assert _infer_occupancy(sqft=None, fp_names=fp_names) == expected


def test_infer_occupancy_sqft_wins_over_fp_names():
    """sqft 优先级高于 fp_names。22 m² 即使 FP 说"Superior Plus Studio"（couples）
    也应该返回 "One"，因为面积是物理硬约束。"""
    fp_names = ["Superior Plus Studio | Furnished | Short Stay"]
    # 仅 fp_names → "Two (only couples)"
    assert _infer_occupancy(fp_names=fp_names) == "Two (only couples)"
    # sqft 主导 → "One"，FP 名字被忽略
    assert _infer_occupancy(sqft="22", fp_names=fp_names) == "One"


def test_extract_units_parses_unit_fields():
    units = _extract_units(UNITS_HTML_1)
    assert len(units) == 2
    assert units[0]["unit_id"] == "307195"
    assert units[0]["apt"] == "#6045"
    assert units[0]["status"] == "Available to book"
    assert units[0]["floor"] == 0
    assert units[0]["avail_date"] == "2026-06-06"
    assert units[1]["status"] == "Available in lottery"
    assert units[1]["floor"] == 1


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        ("Ground Floor, Courtyard View", 0),
        ("Floor 1-4", 1),
        ("Floor 12", 12),
        ("Courtyard View", None),
    ],
)
def test_parse_ourdomain_floor(detail, expected):
    assert parse_ourdomain_floor(detail) == expected


def test_scrape_builds_unit_level_listings(monkeypatch):
    monkeypatch.setattr("scrapers.ourdomain.req.Session", FakeSession)
    monkeypatch.setattr("scrapers.ourdomain.get_impersonate", lambda: "chrome131")
    monkeypatch.setattr("scrapers.ourdomain.get_proxy_url", lambda: "")

    task = ScrapeTask(
        source="ourdomain",
        city_key="diemen",
        city_display="Amsterdam Diemen",
        extra={"move_in_date": "2026-06-01"},
    )
    result = OurDomainScraper().scrape(task)

    assert result.complete is True
    assert len(result.listings) == 2

    first = result.listings[0]
    assert first.id == "od_307195"
    assert first.source == "ourdomain"
    assert first.name == "Diemen #6045"
    assert "22m²" not in first.name
    assert first.status == "Available to book"
    assert first.price_raw == "€ 1.587"
    assert first.price_value == 1587.0
    assert first.available_from == "2026-06-06"
    assert first.city == "Amsterdam Diemen"
    fm = first.feature_map()
    assert fm["unit"] == "#6045"
    assert fm["area"] == "22 m²"
    assert fm["floor"] == "0"
    assert fm["deposit"] == "€ 2.622"
    assert fm["type"] == "Studio"
    assert fm["floorplans"] == "1107060, 1106316"
    # Occupancy 反推：FP 1107060 = "Superior Studio 1-person max."（One）
    # + FP 1106316 = "Executive Studio"（One），都映射到 One，结果 One。
    assert fm["occupancy"] == "One"

    second = result.listings[1]
    assert second.id == "od_307302"
    assert second.status == "Available in lottery"
    assert second.price_value == 1563.0


def test_scrape_raises_blocked_error(monkeypatch):
    monkeypatch.setattr("scrapers.ourdomain.req.Session", BlockedSession)
    monkeypatch.setattr("scrapers.ourdomain.get_impersonate", lambda: "chrome131")
    monkeypatch.setattr("scrapers.ourdomain.get_proxy_url", lambda: "")
    monkeypatch.setenv("OURDOMAIN_WAF_RETRIES", "2")
    monkeypatch.setenv("OURDOMAIN_IMPERSONATES", "chrome131,safari17_0")

    task = ScrapeTask(source="ourdomain", city_key="diemen", city_display="Amsterdam Diemen")
    with pytest.raises(BlockedError) as exc:
        OurDomainScraper().scrape(task)
    assert "chrome131, safari17_0" in str(exc.value)


def test_scrape_retries_cloudflare_with_next_tls_fingerprint(monkeypatch):
    FirstBlockedThenOkSession.instance_count = 0
    FirstBlockedThenOkSession.instances = []
    monkeypatch.setattr("scrapers.ourdomain.req.Session", FirstBlockedThenOkSession)
    monkeypatch.setattr("scrapers.ourdomain.get_proxy_url", lambda: "")
    monkeypatch.setenv("OURDOMAIN_WAF_RETRIES", "2")
    monkeypatch.setenv("OURDOMAIN_IMPERSONATES", "chrome131,safari17_0")

    task = ScrapeTask(
        source="ourdomain",
        city_key="diemen",
        city_display="Amsterdam Diemen",
        extra={"move_in_date": "2026-06-01"},
    )
    result = OurDomainScraper().scrape(task)

    assert len(result.listings) == 2
    assert [s.impersonate for s in FirstBlockedThenOkSession.instances] == [
        "chrome131",
        "safari17_0",
    ]


# ─── data-label fallback（应对 South-East 等替代主题）────────────────


class TestCellExtractionFallback:
    """selenium-id 不匹配时，应该用 data-label 兜底。"""

    def test_selenium_id_wins_when_present(self):
        row = '<td data-selenium-id="SqFt1" data-label="Sq.M.">27</td>'
        assert _cell_text(row, "SqFt1", labels=("Sq.M.",)) == "27"

    def test_falls_back_to_data_label_when_selenium_id_missing(self):
        """South-East 主题：selenium-id 不同，但 data-label 仍是 Sq.M.。"""
        row = '<td data-selenium-id="SomeOtherId" data-label="Sq.M.">31</td>'
        assert _cell_text(row, "SqFt1", labels=("Sq.M.",)) == "31"

    def test_falls_back_through_multiple_labels(self):
        """允许多个 label fallback——主题可能用 Sq.Ft. 或 Size。"""
        row = '<td data-selenium-id="X" data-label="Size">25</td>'
        assert _cell_text(row, "SqFt1", labels=("Sq.M.", "Sq.Ft.", "Size")) == "25"

    def test_no_label_no_selenium_id_returns_empty(self):
        row = '<td data-selenium-id="X" data-label="Other">99</td>'
        assert _cell_text(row, "SqFt1", labels=("Sq.M.",)) == ""

    def test_southeast_style_row_extracts_all_fields(self):
        """模拟 South-East 风格：selenium-id 不同但 data-label 标准。"""
        row = (
            "<tr data-selenium-id='urow1' id='unitrow_551'>"
            "<th data-selenium-id='Unit_1' data-label='Apartment'>#551</th>"
            "<td data-selenium-id='Sq_1' data-label='Sq.M.'>33</td>"
            "<td data-selenium-id='RentVal_1' data-label='Rent'>€ 1.890</td>"
            "<td data-selenium-id='Dep_1' data-label='Deposit'>€ 3.000</td>"
            "<td data-selenium-id='Am_1' data-label='Amenities'>"
            "<label>High Floor</label></td>"
            "<td data-selenium-id='Av_1' data-label='Availability'>"
            "<span class='success'>Available</span></td>"
            "</tr>"
        )
        unit = _extract_unit(row, "551")
        assert unit is not None
        assert unit["apt"] == "#551"
        assert unit["sqft"] == "33", f"sqft 应该从 data-label='Sq.M.' 兜底，实际 {unit['sqft']!r}"
        assert unit["rent"] == "€ 1.890"
        assert unit["deposit"] == "€ 3.000"
        assert "High Floor" in unit["detail"]
        assert unit["status"] == "Available to book"
