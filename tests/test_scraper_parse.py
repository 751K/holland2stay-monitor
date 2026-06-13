"""
scrapers/holland2stay _to_listing 解析测试。

适配新 API（扁平字段，不再有 custom_attributesV2），验证：
- 完整 product item → 正常 Listing 对象
- 缺失字段（price / status / features）→ 降级不抛异常
- 边缘值（null、空字符串、异常类型）
- contract_id / contract_start_date 提取
- attribute ID → label 映射
"""
from __future__ import annotations

import pytest

from scrapers.holland2stay import _to_listing


# ── 辅助：构造最小合法 product item（新 API 扁平字段）─────────────

# 模拟的 attr_labels（ID→label 映射），由 _fetch_attr_labels 产生
_LABELS = {
    "city": {"29": "Eindhoven", "24": "Amsterdam"},
    "no_of_rooms": {"105": "1", "104": "Studio", "6137": "Loft (open bedroom area)"},
    "maximum_number_of_persons": {"23": "Two (only couples)", "22": "One", "500": "Two"},
    "floor": {"6062": "3", "6060": "1", "6059": "0"},
    "finishing": {"71": "Semi furnished", "70": "Fully furnished", "6261": "Furnished"},
    "building_name": {"614": "The Docks"},
    "type_of_contract": {"21": "Indefinite", "20": "1 year max"},
    "tenant_profile_restrictions": {"6124": "student only"},
}


def _item(**overrides):
    """生成最小合法 product item（新 API 扁平字段）。"""
    base = {
        "url_key": "test-listing-1",
        "sku": "TST001",
        "name": "Test Listing 1, Eindhoven",
        "price_range": {
            "minimum_price": {"regular_price": {"value": 850.0}},
        },
        "city": 29,
        "basic_rent": 707,           # int (was "707.000000" string)
        "living_area": "45.0",       # string
        "energy_label": "A",          # string
        "available_to_book": 179,    # int ID
        "available_startdate": "2026-06-01 00:00:00",
        "no_of_rooms": "105",        # string ID ("1")
        "building_name": 614,        # int ID
        "floor": "6062",             # string ID ("3")
        "finishing": 71,             # int ID
        "maximum_number_of_persons": 23,  # int ID
        "type_of_contract": 21,      # int ID
        "next_contract_startdate": None,
        "offer_text_two": "Short-stay",
        "tenant_profile_restrictions": 6124,  # int ID
    }
    base.update(overrides)
    return base


# ── 正常解析 ───────────────────────────────────────────────

class TestToListingNormal:
    def test_full_item_parses_correctly(self):
        listing = _to_listing(_item(), "Eindhoven", _LABELS)
        assert listing is not None
        assert listing.id == "test-listing-1"
        assert listing.sku == "TST001"
        assert listing.name == "Test Listing 1, Eindhoven"
        assert listing.status == "Available to book"
        assert listing.city == "Eindhoven"
        assert listing.price_raw == "€707"
        assert listing.available_from == "2026-06-01"
        assert listing.url == "https://www.holland2stay.com/residences/test-listing-1.html"
        assert listing.contract_id == 21
        assert listing.contract_start_date is None

    def test_features_include_expected_keys(self):
        listing = _to_listing(_item(), "Eindhoven", _LABELS)
        fm = listing.feature_map()
        assert fm["type"] == "1"           # no_of_rooms=105 → "1"
        assert "45.0 m²" in fm["area"]
        assert fm["occupancy"] == "Two (only couples)"
        assert fm["floor"] == "3"          # floor=6062 → "3"
        assert fm["furnishing"] == "Semi furnished"   # finishing=71
        assert fm["energy_label"] == "A"
        assert fm["building"] == "The Docks"
        assert fm["contract"] == "Indefinite"
        assert fm["offer"] == "Short-stay"
        assert fm["tenant"] == "student only"

    def test_lottery_listing_status(self):
        item = _item(available_to_book=336)
        listing = _to_listing(item, "Amsterdam", _LABELS)
        assert listing.status == "Available in lottery"

    def test_contract_start_date_from_next_contract_startdate(self):
        item = _item(next_contract_startdate="2026-07-15 00:00:00")
        listing = _to_listing(item, "Eindhoven", _LABELS)
        assert listing.contract_start_date == "2026-07-15"

    def test_price_uses_price_range_fallback(self):
        """当 basic_rent 缺失时降级到 price_range。"""
        item = _item()
        del item["basic_rent"]
        listing = _to_listing(item, "Eindhoven", _LABELS)
        assert listing.price_raw == "€850"

    def test_available_startdate_datetime_truncation(self):
        item = _item(available_startdate="2026-12-25 23:59:59")
        listing = _to_listing(item, "Eindhoven", _LABELS)
        assert listing.available_from == "2026-12-25"


# ── 缺失字段降级 ────────────────────────────────────────────

class TestToListingMissingFields:
    def test_missing_price_is_none(self):
        item = _item()
        del item["basic_rent"]
        del item["price_range"]
        listing = _to_listing(item, "Eindhoven", _LABELS)
        assert listing.price_raw is None

    def test_missing_status_is_unknown(self):
        item = _item()
        del item["available_to_book"]
        listing = _to_listing(item, "Eindhoven", _LABELS)
        assert listing.status == "Unknown"

    def test_missing_url_key_falls_back_to_sku(self):
        item = _item()
        del item["url_key"]
        listing = _to_listing(item, "Eindhoven", _LABELS)
        assert listing.id == "TST001"

    def test_missing_available_startdate_is_none(self):
        item = _item()
        del item["available_startdate"]
        listing = _to_listing(item, "Eindhoven", _LABELS)
        assert listing.available_from is None

    def test_missing_optional_features_are_absent(self):
        item = _item(
            no_of_rooms=None,
            living_area=None,
            energy_label=None,
            building_name=None,
            floor=None,
            finishing=None,
            maximum_number_of_persons=None,
            offer_text_two="",
            type_of_contract=None,
            tenant_profile_restrictions=None,
        )
        listing = _to_listing(item, "Eindhoven", _LABELS)
        fm = listing.feature_map()
        assert "type" not in fm
        assert "area" not in fm
        assert listing.status == "Available to book"
        assert listing.price_raw == "€707"


# ── 边缘情况 ────────────────────────────────────────────────

class TestToListingEdgeCases:
    def test_null_field_skipped(self):
        item = _item(energy_label=None, living_area=None)
        listing = _to_listing(item, "Eindhoven", _LABELS)
        assert listing is not None
        fm = listing.feature_map()
        assert "energy_label" not in fm  # null → not added to features
        assert "area" not in fm

    def test_unknown_status_id_shows_unknown_with_id(self):
        item = _item(available_to_book=9999)
        listing = _to_listing(item, "Eindhoven", _LABELS)
        assert "Unknown" in listing.status

    def test_corrupt_data_returns_none(self):
        result = _to_listing(None, "Nowhere", _LABELS)  # type: ignore[arg-type]
        assert result is None

    def test_basic_rent_precision(self):
        """basic_rent 整数直接格式化。"""
        item = _item(basic_rent=851)
        listing = _to_listing(item, "Eindhoven", _LABELS)
        assert listing.price_raw == "€851"

    def test_attr_label_fallback_to_raw_id(self):
        """映射缺失时返回原始 ID 值。"""
        item = _item(finishing=9999)  # unknown ID, no mapping
        listing = _to_listing(item, "Eindhoven", _LABELS)
        fm = listing.feature_map()
        assert fm["furnishing"] == "9999"  # raw ID fallback

    def test_empty_labels_dict_uses_raw_ids(self):
        item = _item()
        listing = _to_listing(item, "Eindhoven", {})
        fm = listing.feature_map()
        # all IDs shown as raw values
        assert fm["type"] == "105"
        assert fm["floor"] == "6062"
