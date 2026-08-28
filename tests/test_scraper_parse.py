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
        # price_range（到手价）优先于 basic_rent（基础租金），见 TestPrice
        assert listing.price_raw == "€850"
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

    def test_price_prefers_the_all_in_figure(self):
        """``price_range`` 是到手价，``basic_rent`` 是基础租金，取前者。

        2026-08-28 在生产实测七条 Eindhoven 房源，两者相差 15%–38%
        （707 → 966、780 → 1076），差额量级正是服务费加水电月付。取错的后果
        不只是显示偏低：租金筛选跨 source 共用同一个数，H2S 报基础租金会把
        如实报价的其它平台挤出结果。
        """
        item = _item()
        assert item["basic_rent"] != 850, "fixture 两个价格必须不同，否则测不出取的是哪个"
        listing = _to_listing(item, "Eindhoven", _LABELS)
        assert listing.price_raw == "€850"

    def test_price_falls_back_to_basic_rent(self):
        """``price_range`` 是 Magento 的嵌套结构，任一层缺失就取不到。

        这时宁可报一个偏低的价格，也好过没有价格——没有价格的房源会被所有带
        租金上限的筛选直接漏掉，整条不可见。
        """
        item = _item()
        del item["price_range"]
        listing = _to_listing(item, "Eindhoven", _LABELS)
        assert listing.price_raw == "€707"

    def test_price_falls_back_when_the_nested_shape_is_broken(self):
        """缺的不一定是最外层，也不一定是"缺"。

        三种坏法抛的异常各不相同，只捕获其中一种，另外两种会让整条房源解析
        崩掉——而 GraphQL 的 schema 变过不止一次（见 h2s_gql.py 的字段白名单）。
        所以三种都要覆盖：

            少一层键        -> KeyError
            某层不是 dict   -> TypeError
            value 不是数字  -> ValueError
        """
        broken_shapes = [
            ({}, "KeyError"),
            ({"minimum_price": {}}, "KeyError"),
            ({"minimum_price": {"regular_price": {}}}, "KeyError"),
            (None, "TypeError"),
            ({"minimum_price": None}, "TypeError"),
            ({"minimum_price": []}, "TypeError"),
            ({"minimum_price": {"regular_price": {"value": "n/a"}}}, "ValueError"),
        ]
        for broken, why in broken_shapes:
            item = _item()
            item["price_range"] = broken
            listing = _to_listing(item, "Eindhoven", _LABELS)
            assert listing.price_raw == "€707", f"{why}（{broken!r}）没有回落到 basic_rent"

    def test_broken_basic_rent_does_not_crash(self):
        """兜底本身也可能是坏的，两条路都断时给 None，不要抛。"""
        item = _item(basic_rent="n/a")
        del item["price_range"]
        listing = _to_listing(item, "Eindhoven", _LABELS)
        assert listing.price_raw is None

    def test_zero_all_in_price_falls_back_too(self):
        """0 不是一个价格。直接格式化会渲染出「€0」，比没有价格更误导。"""
        item = _item()
        item["price_range"] = {"minimum_price": {"regular_price": {"value": 0}}}
        listing = _to_listing(item, "Eindhoven", _LABELS)
        assert listing.price_raw == "€707"

    def test_allowance_price_is_never_used(self):
        """``allowance_price`` 是算房补资格的口径，不是租金。

        实测里它既可能远低于基础租金（707 → 431），也可能是 0（租金超过房补
        上限时）。拿它当价格显示会给出一个谁都付不到的数。
        """
        item = _item()
        item["allowance_price"] = 431
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
        assert listing.price_raw == "€850"


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

    def test_price_is_rounded_to_whole_euros(self):
        """Magento 的 value 是浮点，租金不显示小数。"""
        item = _item()
        item["price_range"] = {"minimum_price": {"regular_price": {"value": 850.6}}}
        listing = _to_listing(item, "Eindhoven", _LABELS)
        assert listing.price_raw == "€851"

    def test_basic_rent_precision(self):
        """回落路径同样取整。"""
        item = _item(basic_rent=851)
        del item["price_range"]
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
