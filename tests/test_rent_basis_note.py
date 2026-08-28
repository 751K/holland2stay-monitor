"""OurDomain / OurCampus 的价格是基础租金，必须标注出来。

这两家的 RentCafe ``Rent`` 列不含服务费：OurDomain 官网逐户型分开列 "Base rent"
与 "Service costs"，OurCampus 写明 "excluding service costs and utility advances"。
服务费的量级不小——OurDomain €192–380、OurCampus €282–401——用户看到 €1.563 会
以为那就是到手价。

为什么不像 Xior 那样并进价格：服务费按户型变，而 RentCafe 的 feed 给不出单元的
户型（每个单元都被关联到该楼全部户型 ID，单元行也没有户型列）。同一栋楼里
Superior Studio 是 €192、Executive Studio 是 €320–380，而基础租金只差几十块，
面积/租金/装修档位没有一个能分开它们。猜错一档就是近 €190/月的误差。
"""
from __future__ import annotations

import pytest

from models import Listing
from notifier import _rent_note
from scrapers.ourdomain import (
    SERVICE_COST_RANGES,
    SERVICE_COSTS_DISCOVERED,
    _service_cost_note,
)

OD = "Amsterdam Diemen"
SE = "Amsterdam South-East"
OC = "OurCampus Amsterdam Diemen"


def _listing(source: str, features: list[str]) -> Listing:
    return Listing(
        id="x", name="n", status="Available to book", price_raw="€ 1.563",
        available_from="", url="", city="Amsterdam Diemen",
        source=source, features=features,
    )


class TestRegistry:
    @pytest.mark.parametrize("building", [OD, SE, OC])
    def test_registered_buildings_have_a_note(self, building):
        assert _service_cost_note(building)

    @pytest.mark.parametrize("building", ["Eindhoven", "Rotterdam", "", None])
    def test_unregistered_buildings_get_nothing(self, building):
        """没登记时**不写**一句含糊的「另有服务费」。

        那种文案无法校对、也给不出量级，用户拿它做不了任何判断，只会以为我们
        知道而没说。宁可不标。
        """
        assert _service_cost_note(building or "") is None

    @pytest.mark.parametrize("k", sorted(SERVICE_COST_RANGES))
    def test_ranges_are_ordered_and_plausible(self, k):
        lo, hi = SERVICE_COST_RANGES[k]["range"]
        assert 50 <= lo < hi <= 600, f"{k} 的区间 {lo}–{hi} 不像月度服务费"

    def test_discovery_date_is_recorded(self):
        """服务费会调，至少要留下「什么时候采的」。"""
        assert SERVICE_COSTS_DISCOVERED

    def test_diemen_note_includes_the_separate_heating_charge(self):
        """Diemen 的 Recoverable **不含**公寓供暖，页面估计月均约 €85。

        只报 €248–361 就是少说了 €85——正是这次要修的那种毛病。
        """
        note = _service_cost_note(OD)
        assert "85" in note and "heating" in note

    def test_buildings_without_a_separate_heating_charge_do_not_mention_it(self):
        for b in (SE, OC):
            assert "heating" not in _service_cost_note(b)

    def test_note_carries_the_actual_numbers(self):
        """标注必须带量级。"这里另有服务费"等于没说。"""
        note = _service_cost_note(OD)
        assert "248" in note and "361" in note


class TestScraperAttachesTheFeature:
    """从 scraper 到 feature 这一环必须有用例，否则中间断了不会红。

    变异测试逼出来的：把 _to_listing 里挂 feature 的那三行删掉，下游全部用例
    照样绿——因为它们都是拿手搓的 Listing 测的，没人走过真正的解析路径。
    """

    UNIT = {"unit_id": "307302", "apt": "#6222", "sqft": "23",
            "rent": "€ 1.563", "deposit": "€ 0", "detail": "Floor 1-4",
            "floor": "1", "fp_ids": ["1107060", "1106321"]}

    def _build(self, city_display: str, source: str = "ourdomain"):
        from scrapers.ourdomain import _to_listing
        return _to_listing(
            dict(self.UNIT), base_url="https://example.test",
            city_display=city_display, source=source,
        )

    def test_ourdomain_listing_gets_the_feature(self):
        l = self._build(OD)
        assert "Service costs: €248–361 + ~€85 heating excl." in l.features
        assert l.rent_basis_note

    def test_south_east_is_registered_too(self):
        """South-East 的服务费区间只有 €30 宽，是三栋楼里最确定的一个。"""
        l = self._build(SE)
        assert "Service costs: €174–204 excl." in l.features

    def test_ourcampus_listing_gets_its_own_range(self):
        l = self._build(OC, source="ourcampus")
        assert "Service costs: €282–401 excl." in l.features

    def test_unregistered_building_gets_no_feature(self):
        l = self._build("Rotterdam Nowhere")
        assert not any(f.startswith("Service costs") for f in l.features)
        assert l.rent_basis_note == ""


class TestListingProperty:
    def test_note_reads_from_the_feature(self):
        l = _listing("ourdomain", ["Service costs: €174–204 excl."])
        assert l.rent_basis_note == "base rent, service costs €174–204 excl."

    @pytest.mark.parametrize("source", ["holland2stay", "xior"])
    def test_all_in_sources_carry_no_note(self, source):
        """H2S 与 Xior 的 price_raw 已经是到手价，再标"基础租金"就是假话。"""
        assert _listing(source, ["Type: Studio"]).rent_basis_note == ""

    def test_absent_feature_gives_empty_string(self):
        assert _listing("ourdomain", ["Type: Studio"]).rent_basis_note == ""


class TestNotification:
    @pytest.mark.parametrize("lang,expect", [("zh", "基础租金"), ("en", "base rent")])
    def test_rent_line_carries_the_note(self, lang, expect):
        from notifier import _format_new
        l = _listing("ourdomain", ["Service costs: €248–361 + ~€85 heating excl."])
        text = _format_new(l, lang=lang)
        rent_line = next(x for x in text.splitlines() if "1.563" in x)
        assert expect in rent_line
        assert "248" in rent_line and "361" in rent_line
        assert "85" in rent_line, "供暖那一项在标注里丢了"

    @pytest.mark.parametrize("lang", ["zh", "en"])
    def test_all_in_sources_get_a_clean_rent_line(self, lang):
        from notifier import _format_new
        l = _listing("holland2stay", ["Type: Studio"])
        rent_line = next(x for x in _format_new(l, lang=lang).splitlines() if "1.563" in x)
        assert "base rent" not in rent_line and "基础租金" not in rent_line

    def test_note_is_empty_without_the_feature(self):
        assert _rent_note(_listing("ourdomain", []), "zh") == ""

    def test_excl_marker_is_not_shown_twice(self):
        """feature 里存的是 "€192–380 excl."，展示时那个 excl. 由句子承担。"""
        out = _rent_note(_listing("ourdomain", ["Service costs: €174–204 excl."]), "en")
        assert out.count("excl") <= 1


class TestItDoesNotPolluteTheFilters:
    def test_service_costs_is_not_a_filter_category(self):
        """features 同时喂着筛选下拉。新键要是进了 DEFAULTS，用户表单里会多出
        一栏「服务费」，而那不是可筛的维度。
        """
        from app.i18n import DEFAULTS
        assert "Service costs" not in DEFAULTS
        assert "service_costs" not in DEFAULTS


class TestListingsPageRendering:
    """标注要真的渲染到页面上。

    属性写对了、模板没接，用户还是只看到一个裸价格。这一层单独守。
    """

    @pytest.fixture
    def seeded(self, admin_client):
        from app.db import storage
        st = storage()
        st.diff([
            Listing(id="od_1", name="Diemen #6222", status="Available to book",
                    price_raw="€ 1.563", available_from="", url="https://x",
                    city="Amsterdam Diemen", source="ourdomain",
                    features=["Type: Studio", "Area: 23 m²",
                              "Service costs: €248–361 + ~€85 heating excl."]),
            Listing(id="h2s_1", name="Eindhoven Beukenlaan", status="Available to book",
                    price_raw="€966", available_from="", url="https://y",
                    city="Eindhoven", source="holland2stay",
                    features=["Type: Studio", "Area: 45 m²"]),
        ])
        st.close()
        return admin_client

    def test_the_star_carries_the_range_in_its_tooltip(self, seeded):
        import re
        html = seeded.get("/listings", headers={"Accept-Language": "zh-CN"}).get_data(as_text=True)
        tips = set(re.findall(r'class="rent-basis"[^>]*title="([^"]*)"', html))
        assert tips, "OurDomain 的价格旁没有渲染出口径标注"
        assert all("248" in t and "361" in t for t in tips), tips

    def test_all_in_sources_get_no_star(self, seeded):
        """H2S 的价格已经是到手价，标"基础租金"是假话。

        判据不能只数星号总数——列表页同时渲染桌面表格行和移动端卡片，一条房源
        本来就出现两处。所以断言的是"星号只出现在 OurDomain 那条附近"。
        """
        import re
        html = seeded.get("/listings", headers={"Accept-Language": "zh-CN"}).get_data(as_text=True)
        for m in re.finditer(r'class="rent-basis"', html):
            around = html[max(0, m.start() - 600):m.start()]
            assert "Beukenlaan" not in around.split("Diemen")[-1], "H2S 的价格被标成了基础租金"

    def test_english_tooltip_too(self, seeded):
        import re
        html = seeded.get("/listings", headers={"Accept-Language": "en-US"}).get_data(as_text=True)
        tips = set(re.findall(r'class="rent-basis"[^>]*title="([^"]*)"', html))
        assert tips and all("Base rent" in t for t in tips), tips
