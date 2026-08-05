"""平台不支持某维度时，该条件对它整体跳过——界面上必须说出来。

fail-open 本身是对的：一套过滤条件作用于四个平台，若「白名单匹配不到就拒绝」，
会把整批抓不到该属性的房源误杀。但此前界面上一个字都没提。

后果具体是这样：勾「装修 = Furnished」，H2S 命中 307 条，而 Xior + OurDomain 的
83 条**一条没筛，全放行**。用户以为收到的都是 Furnished，实际不是。
"""
from __future__ import annotations

import pytest

from config import (
    KNOWN_SOURCES,
    ListingFilter,
    dim_scope_badge,
    dim_scope_note,
    sources_supporting_dim,
)
from models import Listing


class TestSupportMatrix:
    def test_h2s_only_dimensions(self):
        for dim in ("contract", "tenant", "offer", "energy", "neighborhood"):
            assert sources_supporting_dim(dim) == ["holland2stay"], dim

    def test_finishing_is_no_longer_h2s_only(self):
        """Xior 与 OurDomain 的装修档位由 SOURCE_ASSUMED_FEATURES 声明。

        声明之后能力表必须跟着登记，否则该维度仍走 fail-open——勾 Unfurnished
        时这些房源照样会出现，而它们恰恰不是无家具的。
        """
        assert sources_supporting_dim("finishing") == [
            "holland2stay", "ourdomain", "xior",
        ]

    def test_xior_lacks_the_rentcafe_dimensions(self):
        for dim in ("floor", "occupancy", "type"):
            assert "xior" not in sources_supporting_dim(dim), dim
            assert "ourdomain" in sources_supporting_dim(dim), dim

    def test_universal_dimensions_cover_everything(self):
        for dim in ("max_rent", "min_area", "city", "source"):
            assert sources_supporting_dim(dim) == list(KNOWN_SOURCES), dim

    def test_unknown_dimension_is_empty(self):
        assert sources_supporting_dim("色号") == []


class TestScopeNote:
    def test_universal_dimension_has_no_note(self):
        """全平台生效的维度不该加提示，否则满屏都是废话。"""
        assert dim_scope_note("max_rent") == ""
        assert dim_scope_badge("max_rent") == ""

    def test_h2s_only_note_names_the_platform(self):
        note = dim_scope_note("energy")
        assert "Holland2Stay" in note
        assert "仅" in note
        # 必须说清楚「其它平台的房源不受影响」，只说「仅对 X 生效」会被读成
        # 「其它平台的房源会被排除」——那是相反的意思
        assert "不受此条件影响" in note

    def test_english_note(self):
        note = dim_scope_note("energy", "en")
        assert "Holland2Stay only" in note
        assert "unaffected" in note

    def test_partial_support_note_lists_all(self):
        note = dim_scope_note("occupancy")
        for name in ("Holland2Stay", "OurDomain", "OurCampus"):
            assert name in note
        assert "Xior" not in note

    def test_badge_is_short(self):
        assert dim_scope_badge("energy") == "仅 Holland2Stay"
        assert dim_scope_badge("energy", "en") == "Holland2Stay only"
        assert len(dim_scope_badge("occupancy")) < 12

    def test_unknown_dimension_gets_no_note(self):
        assert dim_scope_note("色号") == ""
        assert dim_scope_badge("色号") == ""


def _listing(source: str, **features) -> Listing:
    return Listing(
        id="t", name="t", status="Available to book", price_raw="1000",
        available_from="", url="", city="Eindhoven", source=source,
        features=[f"{k}: {v}" for k, v in features.items()],
    )


class TestFailOpenBehaviourIsWhatTheNoteSays:
    """提示写的和代码做的必须是一回事。"""

    def test_unsupported_platform_passes_through(self):
        # 装修不能再拿来举例——Xior 现在有声明值了。合同仍是 H2S 独有。
        f = ListingFilter(allowed_contract=["Indefinite"])
        assert f.passes(_listing("xior")), "Xior 不提供合同类型，应放行"

    def test_supported_platform_still_filtered(self):
        f = ListingFilter(allowed_finishing=["Furnished"])  # H2S 自己上报该属性
        assert f.passes(_listing("holland2stay", Finishing="Furnished"))
        # Unfurnished 是反义词。裸子串匹配会把它一起收走
        # （"Furnished" in "Unfurnished" 为真），词边界匹配才排得掉。
        assert not f.passes(_listing("holland2stay", Finishing="Unfurnished"))

    def test_supported_platform_missing_value_is_rejected(self):
        """平台支持但这条恰好缺值 → 照常拒绝，不削弱该平台的严格度。"""
        f = ListingFilter(allowed_finishing=["Furnished"])
        assert not f.passes(_listing("holland2stay"))

    @pytest.mark.parametrize("dim,kw", [
        ("energy", dict(allowed_energy="A")),
        ("contract", dict(allowed_contract=["Indefinite"])),
        ("tenant", dict(allowed_tenant=["student only"])),
        ("offer", dict(allowed_offer=["Short-stay"])),
    ])
    def test_every_h2s_only_dim_is_fail_open_elsewhere(self, dim, kw):
        assert sources_supporting_dim(dim) == ["holland2stay"]
        assert ListingFilter(**kw).passes(_listing("xior"))
