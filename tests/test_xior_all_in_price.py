"""Xior 的价格必须是租客实际每月付的钱，不是基础租金。

feed 的 ``minimumRent`` 是**基础租金**——楼盘页的房间表列名字面就写着
"Basic rent"，页面另起一段列出 "Monthly Advance Charges"（能源、服务费、
网络、洗衣、家具）。两者相加才是到手价。

这笔预付费各楼从 €180 到 €345 不等，比很多房源之间的价差还大。只报基础租金的
后果不止是显示偏低：租金筛选跨 source 共用同一个数，Xior 会在同一个上限下把
如实报价的平台挤出结果。
"""
from __future__ import annotations

import pytest

from scrapers.xior import XiorScraper, _all_in_price

CHARGES = XiorScraper.MONTHLY_CHARGES
BUILDINGS = XiorScraper.BUILDINGS

#: 有登记、且明细里带家具项的一栋，供减家具的用例用。
ZERNIKESTRAAT = "p0195855"      # total 240, furnishings 50
#: 唯一没有费用表的楼：页面上完全没有 Monthly Advance Charges 区块。
NO_CHARGES = "p0196499"         # Delft Phoenixstraat


class TestRegistry:
    def test_every_building_is_registered_or_knowingly_absent(self):
        """新增楼栋时必须一并登记预付费，否则它会静默按基础租金报价。

        唯一允许缺席的是 Delft Phoenixstraat——它的页面确实没有费用区块。
        写死这一个例外，是为了让"又多了一栋没登记的"变成一条红线而不是沉默。
        """
        missing = set(BUILDINGS) - set(CHARGES)
        assert missing == {NO_CHARGES}, (
            f"这些楼没有登记月度预付费：{sorted(missing)}。"
            "跑 tools/discover_xior_charges.py 补上，或确认它的页面确实没有费用表。"
        )

    def test_no_charges_for_unknown_buildings(self):
        """反向：费用表里不该有 BUILDINGS 里没有的楼，那是改名/下架的残留。"""
        assert not set(CHARGES) - set(BUILDINGS)

    @pytest.mark.parametrize("key", sorted(CHARGES))
    def test_total_is_a_plausible_amount(self, key):
        """实测区间是 €180–€345。落在这之外多半是解析错了，不是真的调价。"""
        total = CHARGES[key]["total"]
        assert 100 <= total <= 500, f"{key} 的 total={total} 不像月度预付费"

    @pytest.mark.parametrize("key", sorted(CHARGES))
    def test_furnishings_is_a_part_of_the_total(self, key):
        """家具项是 TOTAL 的一部分，减完不能变成负数或零。"""
        c = CHARGES[key]
        f = c.get("furnishings")
        if f is None:
            return
        assert 0 < f < c["total"], f"{key} furnishings={f} total={c['total']}"

    def test_discovery_date_is_recorded(self):
        """这张表会随上游调价过期，至少要留下"什么时候采的"。"""
        assert XiorScraper.XIOR_CHARGES_DISCOVERED


class TestAllInPrice:
    def test_adds_the_buildings_monthly_charges(self):
        assert _all_in_price(781, 0, building_key=ZERNIKESTRAAT,
                             furnishing="Fully furnished") == "€1021"

    def test_unfurnished_units_do_not_pay_the_furnishings_item(self):
        """TOTAL 是含家具的，无家具单元不付这笔。"""
        assert _all_in_price(781, 0, building_key=ZERNIKESTRAAT,
                             furnishing="Unfurnished") == "€971"

    def test_unknown_furnishing_keeps_the_full_charge(self):
        """判不出装修档位时宁可多报——低报才是这次要修的毛病。"""
        assert _all_in_price(781, 0, building_key=ZERNIKESTRAAT,
                             furnishing=None) == "€1021"

    def test_semi_furnished_is_not_unfurnished(self):
        """``Semi furnished`` 里含 "furnished"，但它不是无家具。"""
        assert _all_in_price(781, 0, building_key=ZERNIKESTRAAT,
                             furnishing="Semi furnished") == "€1021"

    def test_range_gets_the_same_charge_on_both_ends(self):
        """预付费按楼固定，不随房间大小变。"""
        assert _all_in_price(407, 509, building_key="p0196061",
                             furnishing=None) == "€587–€689"

    def test_unregistered_building_keeps_the_basic_rent(self):
        """缺表时报一个偏低的价格，好过报一个编出来的数。"""
        assert _all_in_price(700, 0, building_key=NO_CHARGES,
                             furnishing=None) == "€700"

    def test_empty_building_key_keeps_the_basic_rent(self):
        assert _all_in_price(700, 0, building_key="", furnishing=None) == "€700"

    def test_no_rent_gives_no_price(self):
        """0 不是价格。渲染成「€240」（纯预付费）比没有价格更误导。"""
        assert _all_in_price(0, 0, building_key=ZERNIKESTRAAT,
                             furnishing=None) is None

    def test_price_is_rounded_to_whole_euros(self):
        """有几栋楼的预付费带小数（€187,75 / €235,50），租金不显示分。"""
        out = _all_in_price(500, 0, building_key="p0196103", furnishing=None)
        assert out == "€688"          # 500 + 187.75 → 687.75 → 688

    def test_single_price_when_min_equals_max(self):
        assert _all_in_price(500, 500, building_key=ZERNIKESTRAAT,
                             furnishing=None) == "€740"


class TestItIsActuallyHigherThanBasicRent:
    """整组的意义是防止有人把逻辑改回基础租金而用例仍然全绿。"""

    @pytest.mark.parametrize("key", sorted(CHARGES))
    def test_every_registered_building_raises_the_price(self, key):
        base = 800
        out = _all_in_price(base, 0, building_key=key, furnishing=None)
        assert out is not None
        value = float(out.lstrip("€").split("–")[0])
        assert value > base, f"{key} 没有把预付费加上去"
        assert value - base == pytest.approx(CHARGES[key]["total"], abs=0.5)
