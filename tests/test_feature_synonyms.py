"""上游同一个属性有荷兰语和英语两版，筛选按字面匹配就会漏。

Holland2Stay 返回哪一版取决于房源录入时的语言，与房源本身无关。同一批数据里
``Two (only couples)`` 134 条、``Twee (alleen koppels)`` 47 条并存，而下拉里两版
并排列着，看起来像两个不同的选项。

2026-08-05 量了一遍（H2S 的 307 条）：

    户型 Loft                  61 → 80    +19
    入住 Two (only couples)   134 → 181   +47
    装修 Furnished            251 → 307   +56
    合同 Indefinite           178 → 234   +56

归一机制（``canonical_feature``）此前就有，但同义表里只有一条 contract，
其余四个维度的匹配也没调用它。
"""
from __future__ import annotations

import logging

import pytest

from config import ListingFilter
from models import FEATURE_SYNONYMS, Listing, canonical_feature


def _listing(**features) -> Listing:
    return Listing(
        id="t", name="t", status="Available to book", price_raw="1000",
        available_from="", url="", city="Eindhoven", source="holland2stay",
        features=[f"{k}: {v}" for k, v in features.items()],
    )


class TestCanonicalFeature:
    @pytest.mark.parametrize("dutch,english", [
        ("Onbepaalde tijd", "Indefinite"),
        ("Twee (alleen koppels)", "Two (only couples)"),
        ("Eén persoon", "One"),
        ("Twee personen", "Two"),
        ("Gezin (ouders met kinderen)", "Family (parents with children)"),
        ("Loft (open slaapkamer)", "Loft (open bedroom area)"),
        ("Gemeubileerd", "Furnished"),
        ("Gestoffeerd", "Semi furnished"),
        ("Alleen voor studenten", "student only"),
    ])
    def test_observed_dutch_values(self, dutch, english):
        """这些是 2026-08-05 在生产库里实际出现过的写法。"""
        assert canonical_feature(dutch) == english

    def test_case_insensitive(self):
        assert canonical_feature("TWEE (ALLEEN KOPPELS)") == "Two (only couples)"
        assert canonical_feature("  gemeubileerd  ") == "Furnished"

    def test_english_values_pass_through(self):
        for v in ("Studio", "1", "Indefinite", "Furnished", "One"):
            assert canonical_feature(v) == v

    def test_unknown_value_passes_through(self):
        assert canonical_feature("Iets heel anders") == "Iets heel anders"

    def test_table_keys_are_lowercase(self):
        """查表用 casefold，键里混大写等于那条永远不生效。"""
        for k in FEATURE_SYNONYMS:
            assert k == k.casefold(), f"键 {k!r} 不是小写"

    def test_no_value_is_also_a_key(self):
        """归一必须一步到位。

        若某个英文值同时又是别的键，``canonical_feature`` 只查一次表，结果取决于
        字典顺序——归一就不再是确定的。
        """
        keys = set(FEATURE_SYNONYMS)
        for v in FEATURE_SYNONYMS.values():
            assert v.casefold() not in keys, f"{v!r} 既是值又是键，会形成二级映射"


class TestFilterMatchesAcrossLanguages:
    def test_occupancy(self):
        f = ListingFilter(allowed_occupancy=["Two (only couples)"])
        assert f.passes(_listing(Occupancy="Twee (alleen koppels)")), \
            "勾了英文那版，收不到荷兰文那版"

    def test_type(self):
        f = ListingFilter(allowed_types=["Loft (open bedroom area)"])
        assert f.passes(_listing(Type="Loft (open slaapkamer)"))

    def test_finishing(self):
        f = ListingFilter(allowed_finishing=["Furnished"])
        assert f.passes(_listing(Finishing="Gemeubileerd"))

    def test_tenant(self):
        f = ListingFilter(allowed_tenant=["student only"])
        assert f.passes(_listing(Tenant="Alleen voor studenten"))

    def test_contract(self):
        f = ListingFilter(allowed_contract=["Indefinite"])
        assert f.passes(_listing(Contract="Onbepaalde tijd"))

    def test_user_side_dutch_also_works(self):
        """老用户存下来的值可能是荷兰语原文，只归一房源侧仍然会漏。"""
        f = ListingFilter(allowed_occupancy=["Twee (alleen koppels)"])
        assert f.passes(_listing(Occupancy="Two (only couples)"))

    def test_still_rejects_a_genuinely_different_value(self):
        """归一是把同义的合并，不是把所有值都放行。"""
        f = ListingFilter(allowed_occupancy=["Two (only couples)"])
        assert not f.passes(_listing(Occupancy="Family (parents with children)"))
        assert not f.passes(_listing(Occupancy="Gezin (ouders met kinderen)"))

    def test_narrower_choice_does_not_widen(self):
        """勾了 Fully furnished 不该收到 Semi furnished。"""
        f = ListingFilter(allowed_finishing=["Fully furnished"])
        assert not f.passes(_listing(Finishing="Gestoffeerd"))


class TestFinishingTiersAreExclusive:
    """装修程度是四档，档与档之间不该互相命中。

    裸子串匹配下，``"Furnished" in "Unfurnished"`` 为真——勾了「有家具」的用户
    会收到无家具的房源，意思正相反；``Fully furnished`` 和 ``Semi furnished``
    也会被一起收走。四档在下拉里各占一项，选哪一档就是哪一档，想要多档就多勾
    几项，这是白名单本来的用法。

    2026-08-05 生产快照（H2S 307 条）：Unfurnished 3 / Semi furnished 45 /
    Furnished 187 / Fully furnished 72，相加正好 307，不重不漏。
    """

    TIERS = ["Unfurnished", "Semi furnished", "Furnished", "Fully furnished"]

    @pytest.mark.parametrize("other", ["Unfurnished", "Semi furnished", "Fully furnished"])
    def test_furnished_does_not_leak_into_other_tiers(self, other):
        f = ListingFilter(allowed_finishing=["Furnished"])
        assert not f.passes(_listing(Finishing=other))

    def test_each_tier_matches_only_itself(self):
        for chosen in self.TIERS:
            f = ListingFilter(allowed_finishing=[chosen])
            for actual in self.TIERS:
                assert f.passes(_listing(Finishing=actual)) is (chosen == actual), \
                    f"勾 {chosen!r} 时 {actual!r} 的判定不对"

    def test_multiple_tiers_can_be_selected(self):
        f = ListingFilter(allowed_finishing=["Furnished", "Fully furnished"])
        assert f.passes(_listing(Finishing="Furnished"))
        assert f.passes(_listing(Finishing="Fully furnished"))
        assert not f.passes(_listing(Finishing="Semi furnished"))

    def test_dutch_values_map_to_the_right_tier(self):
        assert ListingFilter(allowed_finishing=["Furnished"]).passes(
            _listing(Finishing="Gemeubileerd"))
        assert ListingFilter(allowed_finishing=["Semi furnished"]).passes(
            _listing(Finishing="Gestoffeerd"))
        # 归一之后仍然分档，不能因为跨语言就串档
        assert not ListingFilter(allowed_finishing=["Furnished"]).passes(
            _listing(Finishing="Gestoffeerd"))


class TestWordBoundaryMatching:
    """其余维度按词边界匹配，不是裸子串，也不是整体相等。

    整体相等会让跨平台的同一户型对不上：H2S 的房型写 ``1``，OurDomain 写
    ``1-Bedroom Apartment``。
    """

    def test_cross_platform_type_wording(self):
        f = ListingFilter(allowed_types=["1"])
        assert f.passes(_listing(Type="1"))
        assert f.passes(_listing(Type="1-Bedroom Apartment"))

    def test_occupancy_containment_preserved(self):
        f = ListingFilter(allowed_occupancy=["Two"])
        assert f.passes(_listing(Occupancy="Two (only couples)"))

    @pytest.mark.parametrize("pattern,value", [
        # 上游写成复数时，裸子串会把它当成同一个值
        ("Studio", "Studios"),
        # 词尾接续同样不该命中
        ("One", "Oneway"),
        ("Two", "Twofold"),
    ])
    def test_bare_substring_would_over_match(self, pattern, value):
        """守的是规则本身，不是某条现有数据。

        这几个组合眼下不在生产库里，但裸子串对它们全部返回真。上游措辞随时会
        变，规则得先立住——反义词那次（``Furnished`` 命中 ``Unfurnished``）就是
        规则不严的直接后果。
        """
        from config import whitelist_matches
        assert pattern.casefold() in value.casefold(), "前提：裸子串会命中"
        assert not whitelist_matches(pattern, value, "type")

    def test_no_partial_word_match(self):
        # source 必须选一个**仍登记 tenant 维度**的平台。holland2stay 自
        # 2026-08-18 起不再登记（白名单查询里没有 tenant_profile_restrictions，
        # 见 config._SOURCE_FILTER_DIMS），该维度对它 fail-open 整体跳过，
        # 这条断言会变成永远通过——测的就不再是词边界了。
        f = ListingFilter(allowed_tenant=["student only"])
        listing = _listing(Tenant="students onlyish")
        listing.source = "xior"
        assert not f.passes(listing)

    def test_exact_dims_come_from_the_table(self):
        """匹配方式由 _EXACT_MATCH_DIMS 决定，不能在调用点各写各的。

        写成布尔参数时，表可以被清空而行为不变——那张表就退化成了装饰性注释，
        改它不会有任何效果，读它的人却会以为改了有用。
        """
        from config import _EXACT_MATCH_DIMS, whitelist_matches

        assert "finishing" in _EXACT_MATCH_DIMS
        assert not whitelist_matches("Furnished", "Fully furnished", "finishing")
        # 不在表里的维度走词边界，同样的一对值就命中了
        assert whitelist_matches("Furnished", "Fully furnished", "type")

    def test_parentheses_in_pattern_are_literal(self):
        """白名单值里带括号，不能被当成正则分组。"""
        f = ListingFilter(allowed_occupancy=["Two (only couples)"])
        assert f.passes(_listing(Occupancy="Two (only couples)"))
        assert not f.passes(_listing(Occupancy="Two"))

    def test_empty_pattern_matches_nothing(self):
        from config import whitelist_matches
        assert not whitelist_matches("", "Furnished")
        assert not whitelist_matches("   ", "Furnished")
