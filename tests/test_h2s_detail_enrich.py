"""H2S 详情补齐：把白名单主查询拿不到的字段按需取回来。

白名单那条 GetCategories 的字段集里没有 building_name / tenant_profile /
neighborhood / min_income，加进去就是全量 403（docs/H2S.md §5.2）。站点另有一条
**同样在白名单里**的 GetProductDetail，字段集大得多，全都有——2026-08-19 实测放行。

代价是单条约 11.4 KB 且没有分页变量，所以只能按需单取 + 进程内缓存 + 每轮预算，
绝不能拿它替代列表抓取（20 条 160 KB，会把 v1.16.6 省下的流量原样吐回去）。
"""
from __future__ import annotations

import pytest

import scrapers.holland2stay as h2s
from models import Listing


@pytest.fixture(autouse=True)
def _clear_cache():
    """补齐缓存是进程级的，不清会跨用例泄漏，把「只取一次」的断言变成永远通过。"""
    h2s._DETAIL_CACHE.clear()
    yield
    h2s._DETAIL_CACHE.clear()


def _listing(lid="x-1", features=None):
    return Listing(
        id=lid, name=lid, status="Available to book", price_raw="1000",
        available_from="", url="", city="Eindhoven", source="holland2stay",
        features=list(features or []),
    )


_AGGS = [
    {"attribute_code": "building_name", "label": "Building name",
     "options": [{"label": "Philips Bedrijfsschool", "count": 1, "value": "614"}]},
    {"attribute_code": "city", "label": "City", "options": []},
]


class TestDetailFeatures:
    def test_building_id_resolves_via_aggregations(self):
        """building_name 回的是 option ID，label 在同一响应的 aggregations 里。

        因为 filter 收窄到单个 url_key，那个 aggregation 通常只剩这一条选项，
        正好精确对应——不需要跨轮累积映射表。
        """
        got = h2s._detail_features({"building_name": 614}, _AGGS)
        assert got["Building"] == "Philips Bedrijfsschool"

    def test_unknown_building_id_is_skipped_not_guessed(self):
        got = h2s._detail_features({"building_name": 999}, _AGGS)
        assert "Building" not in got

    @pytest.mark.parametrize("tp,expected", [
        (6213, "student only"),
        (6214, "employed only"),
        (6215, "student or employed"),
    ])
    def test_tenant_profile_mapping(self, tp, expected):
        """2026-08-19 从站点**详情页正文**逐条实测确定：

            6213  "Important: Students only"
            6214  "You can book this residence as a working professional"
            6215  "You can book this residence as a student or a working professional"

        曾从下单向导「看到两个选项」推断 6214=两者皆可——那次推断是错的
        （选项渲染出来了但是禁用态）。正文明文才是判据。
        """
        assert h2s._detail_features({"tenant_profile": tp}, [])["Tenant"] == expected

    def test_unknown_tenant_profile_is_skipped(self):
        """上游新增取值时跳过，不瞎猜——猜错会把房源推给不符合资格的用户。"""
        got = h2s._detail_features({"tenant_profile": 9999}, [])
        assert "Tenant" not in got

    def test_neighborhood_and_income(self):
        got = h2s._detail_features(
            {"neighborhood": " Strijp ", "min_income": "3.5"}, [])
        assert got["Neighborhood"] == "Strijp"
        assert got["MinIncome"] == "3.5x rent"

    def test_empty_item_yields_nothing(self):
        assert h2s._detail_features({}, []) == {}


class _Fetcher:
    """按 url_key 回预设详情；记录请求次数。"""

    def __init__(self, per_key=None, boom=False):
        self.per_key = per_key or {}
        self.calls = []
        self.boom = boom

    def fetch_gql(self, query, variables=None, *, operation_name="",
                  extra_headers=None, timeout_ms=30_000):
        key = variables["filters"]["url_key"]["eq"]
        self.calls.append({"key": key, "op": operation_name})
        if self.boom:
            raise RuntimeError("上游炸了")
        item = self.per_key.get(key)
        return {"data": {"products": {
            "items": [item] if item else [],
            "aggregations": _AGGS,
        }}}


class TestEnrich:
    def test_appends_features(self):
        f = _Fetcher({"x-1": {"building_name": 614, "tenant_profile": 6213,
                              "neighborhood": "Strijp"}})
        l = _listing("x-1")
        h2s._enrich(f, [l])
        assert "Building: Philips Bedrijfsschool" in l.features
        assert "Tenant: student only" in l.features
        assert "Neighborhood: Strijp" in l.features

    def test_uses_the_allowlisted_operation_name(self):
        """缺 operationName 或名字不对一律 403。"""
        f = _Fetcher({"x-1": {"building_name": 614}})
        h2s._enrich(f, [_listing("x-1")])
        assert f.calls[0]["op"] == "GetProductDetail"

    def test_fetches_each_listing_only_once(self):
        """进程内缓存。少了它每轮都会为同一批房源重复烧 11 KB。"""
        f = _Fetcher({"x-1": {"building_name": 614}})
        for _ in range(3):
            h2s._enrich(f, [_listing("x-1")])
        assert len(f.calls) == 1

    def test_respects_the_per_round_budget(self):
        """每轮上限。没有它，冷启动会在单轮里把整库 × 11 KB 一次打光。"""
        n = h2s._DETAIL_BUDGET_PER_ROUND
        f = _Fetcher({f"k-{i}": {"building_name": 614} for i in range(n + 10)})
        listings = [_listing(f"k-{i}") for i in range(n + 10)]
        spent = h2s._enrich(f, listings)
        assert spent == n
        assert len(f.calls) == n

    def test_budget_is_not_absurdly_large(self):
        """20 × 11.4 KB ≈ 230 KB/轮。调到三位数就等于放弃了 v1.16.6 的省流量。"""
        assert 1 <= h2s._DETAIL_BUDGET_PER_ROUND <= 50

    def test_failure_is_fail_open_and_not_cached(self):
        """补齐是锦上添花，失败绝不能拖垮主抓取；也不缓存失败，下轮还有机会。"""
        f = _Fetcher(boom=True)
        l = _listing("x-1")
        h2s._enrich(f, [l])          # 不抛
        assert l.features == []
        assert "x-1" not in h2s._DETAIL_CACHE

    def test_does_not_overwrite_existing_features(self):
        """列表查询给的值更新鲜，补齐不该盖掉它。"""
        f = _Fetcher({"x-1": {"building_name": 614}})
        l = _listing("x-1", features=["Building: 列表里已有的"])
        h2s._enrich(f, [l])
        assert l.features == ["Building: 列表里已有的"]

    def test_empty_detail_is_cached_so_we_stop_retrying(self):
        """房源查不到详情（已下架等）也要缓存空结果，否则每轮都白打一次。"""
        f = _Fetcher({})
        for _ in range(3):
            h2s._enrich(f, [_listing("gone")])
        assert len(f.calls) == 1


class TestQueryIsTheVerbatimOne:
    def test_uses_the_copied_document(self):
        """必须用照抄品。裁剪它的字段集实测同样 403（与 GetCategories 一条规律）。"""
        import h2s_booking_gql as gql
        assert h2s._GQL_DETAIL is gql.GETPRODUCTDETAIL
        assert h2s._OP_DETAIL == "GetProductDetail"

    def test_tenant_dim_is_registered_now_that_we_can_fill_it(self):
        """能力表与抓取能力必须同步。

        只改能力表不改抓取 = 勾了「仅学生」的用户一条 H2S 房源都收不到
        （该维度 fail-closed）。这条把两者钉在一起。
        """
        from config import _SOURCE_FILTER_DIMS
        assert "tenant" in _SOURCE_FILTER_DIMS["holland2stay"]
        assert "Tenant" in h2s._detail_features({"tenant_profile": 6213}, [])
