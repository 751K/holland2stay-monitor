"""`city` 这一列在四个平台上存的不是同一种东西。

H2S 存真城市（Eindhoven），Xior / OurDomain / OurCampus 存楼盘名
（Utrecht Willem Dreeslaan、Amsterdam Diemen）。而 `allowed_cities` 是精确
匹配，于是勾了「Utrecht」的用户永远收不到 Xior 在 Utrecht 那 25 套房的通知
——数据在库里、平台也勾了、抓取一切正常，面板上看不出任何异常。

2026-08-05 查线上：14 个用户受影响，累计 56 条房源本该通知而没有通知。

修法是加一层归一：原始值照常保留展示，筛选走归一后的城市。
"""
from __future__ import annotations

import pytest

from config import ListingFilter, canonical_city, known_city_names
from models import Listing


class TestCanonicalCity:
    @pytest.mark.parametrize("raw,want", [
        # Xior：config 里本来就有 city/bldg 拆分，直接用
        ("Utrecht Willem Dreeslaan", "Utrecht"),
        ("Eindhoven Zernikestraat", "Eindhoven"),
        ("Amsterdam Naritaweg", "Amsterdam"),
        ("Groningen Eendrachtskade", "Groningen"),
        # OurDomain / OurCampus
        ("Amsterdam Diemen", "Amsterdam"),
        ("Amsterdam South-East", "Amsterdam"),
        ("OurCampus Amsterdam Diemen", "Amsterdam"),
        # H2S 本来就是城市名，原样通过
        ("Eindhoven", "Eindhoven"),
        ("The Hague", "The Hague"),
    ])
    def test_known_locations(self, raw, want):
        assert canonical_city(raw) == want

    def test_prefix_guessing_would_be_wrong(self):
        """不能靠前缀猜城市。

        「Aachen Vaals Katzensprung」的城市是「Aachen Vaals」不是「Aachen」，
        猜一次错一次；猜错会把房源归到一个不存在的城市，比不归一还糟。
        """
        assert canonical_city("Aachen Vaals Katzensprung") == "Aachen Vaals"

    @pytest.mark.parametrize("raw", ["", "   "])
    def test_blank_stays_blank(self, raw):
        assert canonical_city(raw) == ""

    def test_unknown_value_passes_through(self):
        """没收录的新楼盘原样返回——宁可维持现状，也不要猜出一个错误的城市。"""
        assert canonical_city("Rotterdam Nieuwe Toren") == "Rotterdam Nieuwe Toren"

    def test_case_and_whitespace_insensitive(self):
        assert canonical_city("  utrecht willem dreeslaan  ") == "Utrecht"

    def test_h2s_city_that_is_also_a_prefix_is_untouched(self):
        """Diemen 是 H2S 的独立城市，不能被 Amsterdam Diemen 的映射带走。"""
        assert canonical_city("Diemen") == "Diemen"


class TestKnownCityNames:
    def test_includes_xior_only_cities(self):
        """筛选下拉必须覆盖全平台。

        原先只用 KNOWN_CITIES（H2S 的 26 个），Xior 独有的 Wageningen /
        Venlo / Breda / Leeuwarden 根本不在选项里——用户既选不到，一旦设了
        城市筛选，这些楼盘的房源就被整体挡掉。
        """
        names = set(known_city_names())
        for city in ("Wageningen", "Venlo", "Breda", "Leeuwarden"):
            assert city in names, f"{city} 不在城市选项里"

    def test_contains_no_building_names(self):
        names = known_city_names()
        for n in names:
            assert canonical_city(n) == n, f"{n!r} 是楼盘名，不该出现在城市选项里"

    def test_sorted_and_deduped(self):
        names = known_city_names()
        assert names == sorted(set(names))


def _listing(city: str, source: str) -> Listing:
    return Listing(
        id=f"{source}-{city}", name="x", status="Available to book",
        price_raw="1000", available_from="", features=[], url="",
        city=city, source=source,
    )


class TestNotificationFilter:
    """这条路管的是发不发通知，比列表页严重。"""

    def test_city_matches_building_in_that_city(self):
        f = ListingFilter(allowed_cities=["Utrecht"])
        assert f.passes(_listing("Utrecht Willem Dreeslaan", "xior")), \
            "勾了 Utrecht 仍然收不到 Utrecht 的 Xior 房源"

    def test_amsterdam_matches_ourdomain_buildings(self):
        """线上 Yixin 的实际情形：勾 Amsterdam，17 条 OurDomain 房源收不到。"""
        f = ListingFilter(allowed_cities=["Amsterdam"])
        assert f.passes(_listing("Amsterdam Diemen", "ourdomain"))
        assert f.passes(_listing("Amsterdam South-East", "ourdomain"))
        assert f.passes(_listing("Amsterdam Naritaweg", "xior"))

    def test_stored_building_name_still_works(self):
        """存量配置里存的是楼盘名，不能因为改了判据就失效。"""
        f = ListingFilter(allowed_cities=["Amsterdam Diemen"])
        assert f.passes(_listing("Amsterdam Diemen", "ourdomain"))
        # 归一之后它等价于 Amsterdam，同城的其它楼盘也一并放行
        assert f.passes(_listing("Amsterdam", "holland2stay"))

    def test_other_cities_still_excluded(self):
        """归一是把同城的合并，不是把所有城市合并。"""
        f = ListingFilter(allowed_cities=["Utrecht"])
        assert not f.passes(_listing("Eindhoven", "holland2stay"))
        assert not f.passes(_listing("Eindhoven Zernikestraat", "xior"))

    def test_diemen_does_not_leak_into_amsterdam(self):
        """勾 Diemen（H2S 的独立城市）不该收到 Amsterdam 的房。"""
        f = ListingFilter(allowed_cities=["Diemen"])
        assert f.passes(_listing("Diemen", "holland2stay"))
        assert not f.passes(_listing("Amsterdam", "holland2stay"))

    def test_empty_filter_unaffected(self):
        assert ListingFilter().passes(_listing("Amsterdam Diemen", "ourdomain"))
