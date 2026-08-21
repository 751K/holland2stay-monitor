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
    source_display_name,
    dim_scope_note,
    sources_supporting_dim,
)
from models import Listing


class TestSupportMatrix:
    def test_h2s_only_dimensions(self):
        for dim in ("contract", "offer", "energy", "neighborhood"):
            assert sources_supporting_dim(dim) == ["holland2stay"], dim

    def test_tenant_covers_all_four_platforms(self):
        """租客资格四家全覆盖。**这个断言被来回改过两次，两次都是有原因的。**

        v1.16.2 加这个维度时四家都在。2026-08-18 H2S 上线 operation 白名单，
        我们只能照抄它那条 GetCategories，租客属性不在字段集里——房源不带
        Tenant 标签，而该维度 fail-closed（缺值即拒绝），留着登记会让勾
        「仅学生」的用户一条 H2S 房源都收不到。于是摘掉。

        2026-08-19 恢复：站点另有一条同样在白名单里的 GetProductDetail，字段集里
        有 tenant_profile，按需单取即可补齐（scrapers/holland2stay.py 的详情补齐）。

        改这条断言之前先确认**房源真的带上了 Tenant 标签**——只改能力表不改抓取，
        就又回到「勾了就收不到」的那个坑里。
        """
        assert sources_supporting_dim("tenant") == [
            "holland2stay", "ourdomain", "ourcampus", "xior",
        ]

    def test_tenant_badge_is_empty_when_universal(self):
        """四家全覆盖 = 通用维度 = 不该有徽标。

        徽标由 _SOURCE_FILTER_DIMS 动态算出。多一句「仅 N 个平台」比没有更糟——
        用户会据此以为某些平台没在按资格过滤。
        """
        assert dim_scope_badge("tenant") == ""
        assert dim_scope_note("tenant") == ""

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

    def test_badge_names_platforms_never_just_a_count(self):
        """「仅 3 个平台」既不说是哪 3 个也不说缺谁。

        2026-08-21 实际被误读成「和 Holland2Stay 有关」——而那几个维度恰恰是
        Holland2Stay 全都支持的，缺的是 Xior。徽标必须点名。
        """
        from config import KNOWN_SOURCES
        names = {source_display_name(s) for s in KNOWN_SOURCES}
        for dim in ("type", "occupancy", "finishing", "energy",
                    "contract", "neighborhood", "offer", "floor"):
            for lang in ("zh", "en"):
                badge = dim_scope_badge(dim, lang)
                assert badge, f"{dim} 应当有徽标"
                assert "个平台" not in badge and "platforms" not in badge, (
                    f"{dim}/{lang} 的徽标只报了个数：{badge!r}"
                )
                assert any(n in badge for n in names), (
                    f"{dim}/{lang} 的徽标没点名任何平台：{badge!r}"
                )

    def test_badge_names_the_shorter_side(self):
        """哪边名字少就报哪边——信息量一样，字数更省。"""
        # 缺 1 个（Xior）→ 报缺的
        assert dim_scope_badge("type") == "Xior 除外"
        assert dim_scope_badge("type", "en") == "Except Xior"
        # 缺 1 个（OurCampus）
        assert dim_scope_badge("finishing") == "OurCampus 除外"
        # 只支持 1 个 → 报支持的
        assert dim_scope_badge("contract") == "仅 Holland2Stay"

    def test_badge_and_note_agree_on_which_platforms(self):
        """徽标是 tooltip 的缩写，两者不能各说各的。"""
        for dim in ("type", "finishing", "energy"):
            badge, note = dim_scope_badge(dim), dim_scope_note(dim)
            supported = {source_display_name(s) for s in sources_supporting_dim(dim)}
            for name in supported:
                assert name in note
            if "除外" in badge:
                # 徽标点的是**缺**的那个，它一定不在 note 的支持列表里
                excluded = badge.replace(" 除外", "").split("、")
                assert not (set(excluded) & supported)

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
        ("offer", dict(allowed_offer=["Short-stay"])),
    ])
    def test_every_h2s_only_dim_is_fail_open_elsewhere(self, dim, kw):
        assert sources_supporting_dim(dim) == ["holland2stay"]
        assert ListingFilter(**kw).passes(_listing("xior"))


class TestTenantFiltersEveryPlatform:
    """租客资格登记进能力表之后，四个平台都要真的被筛。

    这是 v1.16.2 的整个目的：此前 allowed_tenant 只对 H2S 生效，勾「仅学生」
    会把 Xior / OurDomain 的房源全部放行——包括那些明确写着 Young
    Professionals only 的。
    """

    def test_xior_is_student_only(self):
        """Xior 的取值来自 SOURCE_ASSUMED_FEATURES，房源自身 features 里没有。"""
        student = ListingFilter(allowed_tenant=["student only"])
        employed = ListingFilter(allowed_tenant=["employed only"])
        xior = _listing("xior", Tenant="student only")
        assert student.passes(xior)
        assert not employed.passes(xior), "勾「仅上班族」不该收到纯学生盘"

    def test_ourdomain_income_only_unit_is_excluded_for_students(self):
        od = _listing("ourdomain", Tenant="employed only")
        assert not ListingFilter(allowed_tenant=["student only"]).passes(od)
        assert ListingFilter(allowed_tenant=["employed only"]).passes(od)

    def test_ourdomain_student_unit_reaches_both_audiences(self):
        """Superior Studio 是「学生（须担保人）+ Young Professionals」两者都收。"""
        od = _listing("ourdomain", Tenant="student and employed")
        assert ListingFilter(allowed_tenant=["student and employed"]).passes(od)

    def test_ourcampus_is_student_only(self):
        oc = _listing("ourcampus", Tenant="student only")
        assert ListingFilter(allowed_tenant=["student only"]).passes(oc)
        assert not ListingFilter(allowed_tenant=["employed only"]).passes(oc)

    def test_missing_value_is_rejected_not_waved_through(self):
        """登记之后就是 fail-closed：缺值即拒绝，与 floor / finishing 一致。

        OurDomain Diemen 偶发拿不到面积时不写 Tenant，这类房源在按资格筛选时
        会被排除。宁可少推不可错推——把「不知道」当成「符合」，会让人白填一轮
        申请。
        """
        od = _listing("ourdomain")
        assert not ListingFilter(allowed_tenant=["student only"]).passes(od)
