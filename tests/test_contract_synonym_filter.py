"""同义合同取值必须在**筛选**层也归一，不只是图表层。

2026-08-04 走查发现：图表早就把 ``Indefinite`` / ``Onbepaalde tijd`` 合成一块了
（``mstorage._charts._merge_synonyms``），但筛选那条路完全没跟上——

- 「合同类型」下拉里两个同义值并排列着，用户根本分不清该勾哪个；
- ``ListingFilter.matches`` 按字面子串比对，勾了 ``Indefinite`` 的用户
  收不到那 38 条写着 ``Onbepaalde tijd`` 的房源。

这类 bug 不报错、不留日志，只是安静地少推送——所以两边都要有断言：
下拉去重，以及**已经存过的**荷兰语原文照样能匹配上。
"""
from __future__ import annotations

import json

from config import ListingFilter
from models import Listing, canonical_feature


def _listing(contract: str) -> Listing:
    return Listing(
        id="x1", name="n", status="Available to book", price_raw="€1",
        available_from="2026-09-01", features=[f"Contract: {contract}"],
        url="u", city="Eindhoven", source="holland2stay",
    )


class TestCanonicalFeature:
    def test_dutch_maps_to_english(self):
        assert canonical_feature("Onbepaalde tijd") == "Indefinite"

    def test_case_and_padding_ignored(self):
        assert canonical_feature("  ONBEPAALDE TIJD ") == "Indefinite"

    def test_unknown_passes_through_trimmed(self):
        assert canonical_feature("  6 months max ") == "6 months max"

    def test_already_canonical_is_stable(self):
        """归一必须幂等，否则链式调用会漂。"""
        assert canonical_feature(canonical_feature("Onbepaalde tijd")) == "Indefinite"


class TestFilterMatching:
    def test_english_selection_matches_dutch_listing(self):
        """这就是线上少推送的那条路径。"""
        f = ListingFilter(allowed_contract=["Indefinite"])
        assert f.passes(_listing("Onbepaalde tijd")) is True

    def test_dutch_selection_still_matches_english_listing(self):
        """老用户库里存的可能是荷兰语原文，不能因为改归一就失效。"""
        f = ListingFilter(allowed_contract=["Onbepaalde tijd"])
        assert f.passes(_listing("Indefinite")) is True

    def test_unrelated_contract_still_filtered_out(self):
        """归一不是"什么都放行"。"""
        f = ListingFilter(allowed_contract=["Indefinite"])
        assert f.passes(_listing("6 months max")) is False

    def test_empty_filter_lets_everything_through(self):
        assert ListingFilter().passes(_listing("Onbepaalde tijd")) is True


class TestStoredValueIsCanonicalised:
    """存进去的值也要归一，否则 iOS 端会出现"看不见的选中项"。

    ``/api/v1/filter/options`` 只返回归一后的 ``Indefinite``；如果用户存的
    还是 ``Onbepaalde tijd``，客户端拿 options 渲染勾选框时找不到这一项，
    界面上 Indefinite 不打勾、实际过滤却在生效。
    """

    def test_dutch_input_is_stored_as_canonical(self):
        f = ListingFilter(allowed_contract=["Onbepaalde tijd"])
        assert f.allowed_contract == ["Indefinite"]

    def test_duplicates_collapse(self):
        """老客户端可能同时勾了两个同义项。"""
        f = ListingFilter(allowed_contract=["Indefinite", "Onbepaalde tijd"])
        assert f.allowed_contract == ["Indefinite"]

    def test_other_values_keep_their_order(self):
        f = ListingFilter(allowed_contract=["6 months max", "Onbepaalde tijd"])
        assert f.allowed_contract == ["6 months max", "Indefinite"]

    def test_empty_stays_empty(self):
        """空列表不能被折腾成别的东西——is_empty() 靠它判断。"""
        f = ListingFilter()
        assert f.allowed_contract == []
        assert f.is_empty() is True


class TestFilterOptions:
    def test_option_list_has_no_duplicate_synonyms(self, tmp_path):
        """下拉里不该同时出现 Indefinite 和 Onbepaalde tijd。"""
        from mstorage import Storage

        st = Storage(tmp_path / "t.db")
        try:
            for i, contract in enumerate(
                ["Indefinite", "Onbepaalde tijd", "6 months max"]
            ):
                st.conn.execute(
                    """INSERT INTO listings
                       (id, name, status, price_raw, available_from, features,
                        url, city, first_seen, last_seen, notified,
                        last_status, source)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (str(i), "n", "Available to book", "\u20ac1", "",
                     json.dumps([f"Contract: {contract}"]), f"u{i}",
                     "Eindhoven", "", "", 0, "Available to book",
                     "holland2stay"),
                )
            st.conn.commit()
            values = st.get_feature_values("Contract")
        finally:
            st.close()

        assert sorted(values) == ["6 months max", "Indefinite"]
