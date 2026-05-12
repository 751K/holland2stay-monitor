"""
scraper _to_listing 解析测试。

覆盖：
- 完整 GraphQL item → 正常 Listing 对象
- 缺失字段（price / status / features）→ 降级不抛异常
- 边缘值（null、空字符串、异常类型）
- contract_id / contract_start_date 提取
"""
from __future__ import annotations

import pytest

from scraper import _to_listing


# ── 辅助：构造最小合法 GraphQL item ──────────────────────────

def _item(**overrides):
    """生成最小 GraphQL product item，调用方可以 overrides 覆盖任意字段。"""
    base = {
        "url_key": "test-listing-1",
        "sku": "TST001",
        "name": "Test Listing 1, Eindhoven",
        "price_range": {
            "minimum_price": {"regular_price": {"value": 850.0}},
        },
        "custom_attributesV2": {
            "items": [
                {"code": "available_to_book", "selected_options": [{"label": "Available to book", "value": "179"}]},
                {"code": "basic_rent", "value": "707.000000"},
                {"code": "price", "value": "850.000000"},
                {"code": "available_startdate", "value": "2026-06-01 00:00:00"},
                {"code": "living_area", "value": "45.0"},
                {"code": "no_of_rooms", "selected_options": [{"label": "2", "value": "2"}]},
                {"code": "building_name", "selected_options": [{"label": "The Docks", "value": "1"}]},
                {"code": "city", "selected_options": [{"label": "Eindhoven", "value": "29"}]},
                {"code": "energy_label", "value": "A"},
                {"code": "finishing", "selected_options": [{"label": "Upholstered", "value": "1"}]},
                {"code": "floor", "selected_options": [{"label": "3", "value": "3"}]},
                {"code": "maximum_number_of_persons", "selected_options": [{"label": "Two (only couples)", "value": "2"}]},
                {"code": "neighborhood", "value": "Strijp-S"},
                {"code": "type_of_contract", "selected_options": [{"label": "Indefinite", "value": "123"}]},
                {"code": "offer_text_two", "value": "Short-stay"},
                {"code": "tenant_profile", "selected_options": [{"label": "student only", "value": "1"}]},
            ],
        },
    }
    base.update(overrides)
    return base


# ── 正常解析 ───────────────────────────────────────────────

class TestToListingNormal:
    def test_full_item_parses_correctly(self):
        listing = _to_listing(_item(), "Eindhoven")
        assert listing is not None
        assert listing.id == "test-listing-1"
        assert listing.sku == "TST001"
        assert listing.name == "Test Listing 1, Eindhoven"
        assert listing.status == "Available to book"
        assert listing.city == "Eindhoven"
        assert listing.price_raw == "€850"
        assert listing.available_from == "2026-06-01"
        assert listing.url == "https://www.holland2stay.com/residences/test-listing-1.html"
        assert listing.contract_id == 123
        assert listing.contract_start_date is None  # next_contract_startdate 未提供

    def test_features_include_expected_keys(self):
        listing = _to_listing(_item(), "Eindhoven")
        fm = listing.feature_map()
        assert fm["type"] == "2"
        assert "45.0 m²" in fm["area"]
        assert fm["occupancy"] == "Two (only couples)"
        assert fm["floor"] == "3"
        assert fm["furnishing"] == "Upholstered"
        assert fm["energy_label"] == "A"
        assert fm["neighborhood"] == "Strijp-S"
        assert fm["building"] == "The Docks"
        assert fm["contract"] == "Indefinite"
        assert fm["offer"] == "Short-stay"
        assert fm["tenant"] == "student only"

    def test_lottery_listing_status(self):
        item = _item()
        item["custom_attributesV2"]["items"] = [
            {"code": "available_to_book", "selected_options": [{"label": "Available in lottery", "value": "336"}]},
        ]
        listing = _to_listing(item, "Amsterdam")
        assert listing.status == "Available in lottery"

    def test_contract_start_date_from_next_contract_startdate(self):
        item = _item()
        item["custom_attributesV2"]["items"].append(
            {"code": "next_contract_startdate", "value": "2026-07-15 00:00:00"},
        )
        listing = _to_listing(item, "Eindhoven")
        assert listing.contract_start_date == "2026-07-15"


# ── 缺失字段降级 ────────────────────────────────────────────

class TestToListingMissingFields:
    def test_missing_price_falls_back_to_price_range(self):
        item = _item()
        # remove both basic_rent and price
        item["custom_attributesV2"]["items"] = [
            a for a in item["custom_attributesV2"]["items"]
            if a["code"] not in ("basic_rent", "price")
        ]
        listing = _to_listing(item, "Eindhoven")
        assert listing.price_raw == "€850"

    def test_missing_all_prices_is_none(self):
        item = _item()
        item["custom_attributesV2"]["items"] = [
            a for a in item["custom_attributesV2"]["items"]
            if a["code"] not in ("basic_rent", "price")
        ]
        item.pop("price_range", None)
        listing = _to_listing(item, "Eindhoven")
        assert listing.price_raw is None

    def test_missing_status_is_unknown(self):
        item = _item()
        item["custom_attributesV2"]["items"] = []
        listing = _to_listing(item, "Eindhoven")
        assert listing.status == "Unknown"

    def test_missing_url_key_falls_back_to_sku(self):
        item = _item()
        item.pop("url_key")
        listing = _to_listing(item, "Eindhoven")
        assert listing.id == "TST001"
        assert listing.url.endswith(".html")

    def test_missing_available_startdate_is_none(self):
        item = _item()
        item["custom_attributesV2"]["items"] = [
            a for a in item["custom_attributesV2"]["items"]
            if a["code"] != "available_startdate"
        ]
        listing = _to_listing(item, "Eindhoven")
        assert listing.available_from is None

    def test_missing_optional_features_are_absent(self):
        item = _item()
        # keep only status + basic_rent
        item["custom_attributesV2"]["items"] = [
            {"code": "available_to_book", "selected_options": [{"label": "Available to book", "value": "179"}]},
            {"code": "basic_rent", "value": "500.000000"},
        ]
        listing = _to_listing(item, "Eindhoven")
        fm = listing.feature_map()
        assert "type" not in fm
        assert "area" not in fm
        assert listing.price_raw == "€500"


# ── 边缘情况 ────────────────────────────────────────────────

class TestToListingEdgeCases:
    def test_null_attribute_value_skipped(self):
        item = _item()
        # add an attribute with null value — should be skipped, not crash
        item["custom_attributesV2"]["items"].append(
            {"code": "energy_label", "value": None},
        )
        listing = _to_listing(item, "Eindhoven")
        assert listing is not None

    def test_empty_selected_options(self):
        item = _item()
        item["custom_attributesV2"]["items"] = [
            {"code": "available_to_book", "selected_options": []},
            {"code": "basic_rent", "value": "500.000000"},
        ]
        listing = _to_listing(item, "Eindhoven")
        assert listing.status == "Unknown"

    def test_missing_custom_attributesV2(self):
        item = _item()
        item.pop("custom_attributesV2")
        listing = _to_listing(item, "Eindhoven")
        assert listing is not None
        assert listing.status == "Unknown"
        assert listing.price_raw == "€850"  # fallback to price_range

    def test_corrupt_data_returns_none(self):
        """严重损坏的 item（非 dict）应抛异常并返回 None。"""
        result = _to_listing(None, "Nowhere")  # type: ignore[arg-type]
        assert result is None

    def test_basic_rent_precision(self):
        """basic_rent 含小数时 f-string :.0f 做四舍五入（half-even）。"""
        item = _item()
        for a in item["custom_attributesV2"]["items"]:
            if a["code"] == "price":
                a["value"] = "851.500000"  # 851.5 rounds to 852 (half-even)
        listing = _to_listing(item, "Eindhoven")
        assert listing.price_raw == "€852"

    def test_available_startdate_datetime_truncation(self):
        """available_startdate 含时间部分，取前 10 字符。"""
        item = _item()
        for a in item["custom_attributesV2"]["items"]:
            if a["code"] == "available_startdate":
                a["value"] = "2026-12-25 23:59:59"
        listing = _to_listing(item, "Eindhoven")
        assert listing.available_from == "2026-12-25"
