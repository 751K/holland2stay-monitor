"""
models.py 的解析函数 + config.ListingFilter.passes() 的过滤语义。

最重要的契约：**fail-closed 原则**。
当过滤条件已设置（如 max_rent=1000）但房源对应字段缺失或无法解析时，
必须返回 False（不通过），绝不能 fail-open（默认通过）。

理由：自动预订是"真实花钱"的操作。如果价格字段缺失就默认放行，
可能误下单一套天价房子。
"""
from __future__ import annotations

import pytest

from config import ListingFilter
from models import Listing, parse_float, parse_int, parse_features_list


def _make_listing(**overrides):
    """构造测试用 Listing，默认填充常规字段，可按需覆盖。"""
    base = dict(
        id="test-1",
        name="Test Listing",
        status="Available to book",
        price_raw="€700",
        available_from="2030-01-01",
        features=["Type: Studio", "Area: 26.0 m²", "Floor: 3",
                  "Occupancy: Single", "Neighborhood: Strijp-S"],
        url="https://h2s/test-1",
        city="Eindhoven",
    )
    base.update(overrides)
    return Listing(**base)


# ─── parse_float / parse_int ──────────────────────────────────────────


class TestParseFloat:
    def test_plain_int(self):
        assert parse_float("700") == 700.0

    def test_with_euro_symbol(self):
        assert parse_float("€707") == 707.0

    def test_with_decimal(self):
        assert parse_float("26.0 m²") == 26.0

    def test_with_thousands_separator(self):
        # "1,200.50" → 1200.50
        assert parse_float("€1,200.50") == 1200.5

    def test_empty_returns_none(self):
        assert parse_float("") is None

    def test_none_returns_none(self):
        assert parse_float(None) is None

    def test_no_digits_returns_none(self):
        assert parse_float("abc") is None
        assert parse_float("Ground floor") is None

    def test_extracts_first_match(self):
        # 多个数字时取第一个
        assert parse_float("€700 + €50 fees") == 700.0


class TestParseInt:
    def test_plain(self):
        assert parse_int("3") == 3

    def test_with_text(self):
        assert parse_int("Floor 3") == 3

    def test_empty(self):
        assert parse_int("") is None

    def test_no_digits(self):
        assert parse_int("Ground floor") is None

    def test_first_integer_only(self):
        # parse_int 用 \d+，不是 \d+\.\d+，所以 "3.5" 提到 3
        assert parse_int("3.5") == 3


class TestParseFeaturesList:
    def test_basic(self):
        d = parse_features_list(["Type: Studio", "Area: 26.0 m²"])
        assert d == {"type": "Studio", "area": "26.0 m²"}

    def test_empty_list(self):
        assert parse_features_list([]) == {}

    def test_skips_malformed_entries(self):
        # 缺少 ": " 的条目被跳过
        d = parse_features_list(["Type: Studio", "MalformedNoColon", "Area: 30 m²"])
        assert d == {"type": "Studio", "area": "30 m²"}

    def test_unknown_key_lowercased(self):
        # LISTING_KEY_MAP 不认识的 key → 用 lower() 兜底
        d = parse_features_list(["CustomKey: value"])
        assert "customkey" in d


# ─── ListingFilter.is_empty ──────────────────────────────────────────


class TestListingFilterIsEmpty:
    def test_default_is_empty(self):
        assert ListingFilter().is_empty() is True

    def test_with_max_rent_not_empty(self):
        assert ListingFilter(max_rent=1000).is_empty() is False

    def test_with_allowed_types_not_empty(self):
        assert ListingFilter(allowed_types=["Studio"]).is_empty() is False

    def test_empty_list_still_empty(self):
        # 设了空列表 == 未设
        assert ListingFilter(allowed_types=[]).is_empty() is True


# ─── ListingFilter.passes 的 fail-closed 语义 ─────────────────────────


class TestListingFilterFailClosed:
    """
    数值字段（max_rent / min_area / min_floor）已设置但房源字段缺失/无法解析时，
    必须返回 False（不通过）。这是预防自动预订误触发的关键防线。
    """

    def test_max_rent_set_but_price_missing(self):
        """已设 max_rent，price_raw=None → 不通过。"""
        f = ListingFilter(max_rent=1000.0)
        l = _make_listing(price_raw=None)
        assert f.passes(l) is False

    def test_max_rent_set_but_price_unparseable(self):
        """price_raw 存在但是无法解析数字 → 不通过。"""
        f = ListingFilter(max_rent=1000.0)
        l = _make_listing(price_raw="价格未知")
        assert f.passes(l) is False

    def test_min_area_set_but_area_feature_missing(self):
        """min_area 设了，但 features 里没有 Area → 不通过。"""
        f = ListingFilter(min_area=20.0)
        l = _make_listing(features=["Type: Studio", "Floor: 3"])
        assert f.passes(l) is False

    def test_min_floor_set_but_floor_feature_missing(self):
        f = ListingFilter(min_floor=2)
        l = _make_listing(features=["Type: Studio", "Area: 25 m²"])
        assert f.passes(l) is False


class TestListingFilterValueChecks:
    """数值字段正向：值合法且满足条件 → 通过。"""

    def test_max_rent_pass(self):
        f = ListingFilter(max_rent=1000.0)
        l = _make_listing(price_raw="€700")
        assert f.passes(l) is True

    def test_max_rent_exact_boundary(self):
        """price == max_rent 视为通过（<=）。"""
        f = ListingFilter(max_rent=700.0)
        l = _make_listing(price_raw="€700")
        assert f.passes(l) is True

    def test_max_rent_exceeded(self):
        f = ListingFilter(max_rent=600.0)
        l = _make_listing(price_raw="€700")
        assert f.passes(l) is False

    def test_min_area_pass(self):
        f = ListingFilter(min_area=20.0)
        l = _make_listing(features=["Area: 26.0 m²"])
        assert f.passes(l) is True

    def test_min_area_below(self):
        f = ListingFilter(min_area=30.0)
        l = _make_listing(features=["Area: 20 m²"])
        assert f.passes(l) is False

    def test_min_floor_pass(self):
        f = ListingFilter(min_floor=2)
        l = _make_listing(features=["Floor: 3"])
        assert f.passes(l) is True

    def test_min_floor_below(self):
        f = ListingFilter(min_floor=5)
        l = _make_listing(features=["Floor: 2"])
        assert f.passes(l) is False


class TestListingFilterWhitelist:
    """字符串白名单：子串匹配 + 大小写不敏感。"""

    def test_allowed_types_substring_match(self):
        # "Studio" 在 "Studio (open)" 里
        f = ListingFilter(allowed_types=["Studio"])
        l = _make_listing(features=["Type: Studio (open bedroom)"])
        assert f.passes(l) is True

    def test_allowed_types_case_insensitive(self):
        f = ListingFilter(allowed_types=["studio"])
        l = _make_listing(features=["Type: Studio"])
        assert f.passes(l) is True

    def test_allowed_types_no_match(self):
        f = ListingFilter(allowed_types=["Loft"])
        l = _make_listing(features=["Type: Studio"])
        assert f.passes(l) is False

    def test_allowed_neighborhoods(self):
        f = ListingFilter(allowed_neighborhoods=["Strijp"])
        l = _make_listing(features=["Neighborhood: Strijp-S"])
        assert f.passes(l) is True

    def test_allowed_cities_exact_match(self):
        """城市是精确匹配（不是子串）。"""
        f = ListingFilter(allowed_cities=["Eindhoven"])
        l = _make_listing(city="Eindhoven")
        assert f.passes(l) is True
        l2 = _make_listing(city="Amsterdam")
        assert f.passes(l2) is False

    def test_empty_whitelist_passes_all(self):
        """白名单是空列表 = 不生效（全部放行）。"""
        f = ListingFilter(allowed_types=[])
        assert f.passes(_make_listing()) is True


class TestListingFilterEmptyFilterPassesAll:
    """完全没设条件 → 任何房源都通过。"""

    def test_empty_filter_passes_normal(self):
        assert ListingFilter().passes(_make_listing()) is True

    def test_empty_filter_passes_missing_fields(self):
        """即使房源字段大量缺失，空 filter 也放行 —— 不与 fail-closed 矛盾。"""
        l = _make_listing(price_raw=None, features=[])
        assert ListingFilter().passes(l) is True


class TestListingFilterCompound:
    """多个条件 AND 关系：全部满足才通过。"""

    def test_all_match(self):
        f = ListingFilter(
            max_rent=1000.0,
            min_area=20.0,
            allowed_types=["Studio"],
        )
        l = _make_listing(
            price_raw="€700",
            features=["Type: Studio", "Area: 26 m²"],
        )
        assert f.passes(l) is True

    def test_one_fails_all_fail(self):
        """三个条件中一个失败 → 整体失败。"""
        f = ListingFilter(
            max_rent=600.0,        # 失败：700 > 600
            min_area=20.0,         # 通过
            allowed_types=["Studio"],  # 通过
        )
        l = _make_listing(
            price_raw="€700",
            features=["Type: Studio", "Area: 26 m²"],
        )
        assert f.passes(l) is False
