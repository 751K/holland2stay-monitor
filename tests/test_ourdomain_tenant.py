"""OurDomain / OurCampus 的租户资格分类（``Tenant`` feature）。

平台把房源分成「学生可租」和「必须有收入」两类，但 **RentCafe 租赁门户完全
不带这个信息**——unit 行只有面积、租金、押金、楼层。判据只存在于平台自己的
官网 Criteria 页，2026-08-08 抓取原文：

    OurDomain Diemen      Superior Studio:      1-person max., Students
                                                (with Guarantors) & Young Professionals
                          Executive Studio:     2-person max., Young Professionals only
                          Superior Plus Studio: 2-person max., Young Professionals only
    OurDomain South East  全篇无学生条款；要雇佣合同或 ZZP，收入 3–4× base rent
    OurCampus Diemen      须在校证明（MBO/HBO/大学），PhD 与博后明确不符合；
                          页脚「Young professionals book at: [OurDomain]」

落到单元级只能靠面积——FP→unit 映射不可用（docs/OURDOMAIN.md §3.3），生产
数据里 #7343（34.43 m²）挂着 Superior Studio 的 FP id 而 #6387（33.78 m²）
没挂，噪声实锤。Diemen 各户型尺寸不重叠，生产库存实测落在两簇：

    22.56 / 27.86 / 28.39 / 29.27 / 30.20  │  33.78 / 34.43

阈值取 32，缺口两侧各留约 1.8 m²。

取值必须落在 ``app.i18n.DEFAULT_TENANT`` 的词汇里——写错一个字母不会报错，
只会得到一个筛不出来的值，且和 H2S 的 ``tenant_profile_restrictions`` 不再
合并。最后一组测试守的就是这件事。
"""
from __future__ import annotations

import pytest

from scrapers.ourcampus import OurCampusScraper
from scrapers.ourdomain import OurDomainScraper, _infer_tenant, _to_listing

# 用 .get()：某栋楼漏配规则时，该由 TestEveryBuildingHasAPolicy 给出可读的
# 失败信息，而不是在收集阶段抛 KeyError 让整个文件都跑不起来。
DIEMEN = OurDomainScraper.BUILDINGS["diemen"].get("tenant_policy")
SOUTH_EAST = OurDomainScraper.BUILDINGS["south-east"].get("tenant_policy")
CAMPUS = OurCampusScraper.BUILDINGS["diemen"].get("tenant_policy")

# 生产库存实测到的面积（2026-08-08，24 个单元）
STUDENT_SIZES = ["22,56", "27,86", "28,39", "29,27", "30,20"]
INCOME_SIZES = ["33,78", "34,43"]


class TestDiemenSplitsBySize:
    @pytest.mark.parametrize("sqft", STUDENT_SIZES)
    def test_superior_studio_admits_students(self, sqft):
        assert _infer_tenant(DIEMEN, sqft) == "student and employed"

    @pytest.mark.parametrize("sqft", INCOME_SIZES)
    def test_larger_floorplans_are_income_only(self, sqft):
        assert _infer_tenant(DIEMEN, sqft) == "employed only"

    def test_threshold_sits_inside_the_gap(self):
        """阈值必须落在 30.20 与 33.78 之间，且两侧都有余量。

        直接断言 32.0 只是复读常量。这里断言的是它相对**实测数据**的位置——
        真实户型尺寸变了，这条会失败，而那正是该重新核对 criteria 的时候。
        """
        from models import parse_float
        from scrapers.ourdomain import _TENANT_STUDENT_MAX_SQM as T

        biggest_student = max(parse_float(s) for s in STUDENT_SIZES)
        smallest_income = min(parse_float(s) for s in INCOME_SIZES)
        assert biggest_student < T < smallest_income
        assert T - biggest_student >= 1.0
        assert smallest_income - T >= 1.0

    def test_unknown_size_does_not_guess(self):
        """面积缺失时宁可不写。

        猜「要收入」会让够资格的学生看不到房；猜「学生可」会让人白填一轮
        申请。两种错都比没有这个字段更糟。
        """
        assert _infer_tenant(DIEMEN, "") is None
        assert _infer_tenant(DIEMEN, "n/a") is None


class TestWholeBuildingPolicies:
    @pytest.mark.parametrize("sqft", STUDENT_SIZES + INCOME_SIZES + [""])
    def test_south_east_is_income_only_regardless_of_size(self, sqft):
        """SE 各户型尺寸严重重叠（20.8–21.4 / 20.8–26.2 / 26.5–33.5 …），
        但整栋都没有学生档，所以不按面积切，也不该受面积影响。"""
        assert _infer_tenant(SOUTH_EAST, sqft) == "employed only"

    @pytest.mark.parametrize("sqft", STUDENT_SIZES + INCOME_SIZES + [""])
    def test_ourcampus_is_student_only_regardless_of_size(self, sqft):
        assert _infer_tenant(CAMPUS, sqft) == "student only"

    def test_no_policy_yields_nothing(self):
        """新楼栋忘了配规则时不要凭空造一个值。"""
        assert _infer_tenant(None, "22,56") is None
        assert _infer_tenant({}, "22,56") is None


class TestEveryBuildingHasAPolicy:
    def test_all_buildings_declare_one(self):
        """新增楼栋必须显式声明资格规则。漏掉不会报错，只是整栋房源静默
        地少一个筛选维度——用户看不出区别，只会觉得筛选不准。"""
        missing = []
        for cls in (OurDomainScraper, OurCampusScraper):
            for key, b in cls.BUILDINGS.items():
                if not b.get("tenant_policy"):
                    missing.append(f"{cls.source}:{key}")
        assert not missing, f"这些楼栋没有 tenant_policy: {missing}"


class TestValuesAreFilterable:
    """取值必须是 Web 筛选认识的词。"""

    def test_values_are_in_the_shared_vocabulary(self):
        from app.i18n import DEFAULT_TENANT

        produced = set()
        for policy in (DIEMEN, SOUTH_EAST, CAMPUS):
            for k in ("default", "student"):
                if policy.get(k):
                    produced.add(policy[k])
        unknown = sorted(produced - set(DEFAULT_TENANT))
        assert not unknown, (
            f"这些取值不在 app.i18n.DEFAULT_TENANT 里，筛不出来也没有中文: {unknown}"
        )

    def test_values_have_localized_labels(self, app_ctx):
        from app.i18n import localize_options

        for policy in (DIEMEN, SOUTH_EAST, CAMPUS):
            for k in ("default", "student"):
                v = policy.get(k)
                if not v:
                    continue
                pairs = localize_options("Tenant", [v])
                label = pairs[0][1] if isinstance(pairs[0], tuple) else pairs[0]
                assert label and label != v, f"{v!r} 没有本地化标签"


class TestListingCarriesTheFeature:
    @staticmethod
    def _unit(sqft: str) -> dict:
        return {
            "unit_id": "307195", "apt": "#6045", "sqft": sqft,
            "rent": "€ 1.587", "deposit": "€ 2.277", "detail": "Floor 1-4",
            "floor": 1, "status": "Available to book",
            "avail_date": "2026-09-01", "fp_ids": [],
        }

    def _features(self, sqft: str, policy) -> list[str]:
        return _to_listing(
            self._unit(sqft), base_url="https://x/", city_display="Amsterdam Diemen",
            source="ourdomain", tenant_policy=policy,
        ).features

    def test_student_unit_tagged(self):
        assert "Tenant: student and employed" in self._features("22,56", DIEMEN)

    def test_income_unit_tagged(self):
        assert "Tenant: employed only" in self._features("33,78", DIEMEN)

    def test_prefix_matches_h2s(self):
        """必须是 ``Tenant: `` 前缀——Web 的 get_feature_values("Tenant")
        靠它 distinct，前缀写错等于这个维度对 OD 不存在。"""
        feats = self._features("22,56", DIEMEN)
        assert sum(f.startswith("Tenant: ") for f in feats) == 1

    def test_no_feature_without_size(self):
        assert not [f for f in self._features("", DIEMEN) if f.startswith("Tenant")]

    def test_no_feature_without_policy(self):
        assert not [f for f in self._features("22,56", None) if f.startswith("Tenant")]
