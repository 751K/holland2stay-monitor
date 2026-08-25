"""H2S 详情补齐：把白名单主查询拿不到的字段按需取回来。

白名单那条 GetCategories 的字段集里没有 building_name / tenant_profile /
neighborhood / min_income，加进去就是全量 403（docs/H2S.md §5.2）。站点另有一条
**同样在白名单里**的 GetProductDetail，字段集大得多，全都有——2026-08-19 实测放行。

代价是单条约 11.4 KB 且没有分页变量，所以只能按需单取 + 进程内缓存 + 每轮预算，
绝不能拿它替代列表抓取（20 条 160 KB，会把 v1.16.6 省下的流量原样吐回去）。
"""
from __future__ import annotations

import json

import pytest

import scrapers.holland2stay as h2s
from models import Listing


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """补齐会在请求之间 sleep（防 429）。测试里不能真等——70 条就是 41 秒。"""
    monkeypatch.setattr(h2s.time, "sleep", lambda _s: None)


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

    def test_budget_still_has_a_ceiling(self):
        """上限兜住「上游忽然返回几百条」——一次 300 条就是 3.4 MB。"""
        assert h2s._DETAIL_BUDGET_PER_ROUND <= 200

    def test_failure_is_fail_open_and_not_cached(self):
        """补齐是锦上添花，失败绝不能拖垮主抓取；也不缓存失败，下轮还有机会。"""
        f = _Fetcher(boom=True)
        l = _listing("x-1")
        h2s._enrich(f, [l])          # 不抛
        assert l.features == []
        assert "x-1" not in h2s._DETAIL_CACHE

    def test_failures_are_logged_loudly_not_silently(self, caplog):
        """fail-open + debug 日志 = 静默半残。

        实测踩过：46 条只补上 25 条，日志上完全看不出来，因为失败写的是 debug。
        补齐失败不是无害的——那些房源这轮没有 Tenant 标签，会被 fail-closed 的
        租客筛选**拒掉**，用户少收房源却无从察觉。必须在 WARNING 级别可见。
        """
        import logging
        f = _Fetcher(boom=True)
        with caplog.at_level(logging.WARNING, logger=h2s.logger.name):
            h2s._enrich(f, [_listing("x-1"), _listing("x-2")])
        text = caplog.text
        assert "详情补齐" in text and "失败" in text, f"失败没有在 WARNING 级别报出来: {text!r}"
        assert "2" in text, "没报出失败条数"

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


class TestRateLimiting:
    """一口气连发详情请求会被 H2S 打 429 —— 2026-08-20 生产实测，46 条里 24 条失败。

    **限速比限量更重要**：光有条数上限挡不住 429，因为 429 是按速率触发的。
    """

    @pytest.fixture(autouse=True)
    def _record_sleep(self, monkeypatch):
        slept = []
        monkeypatch.setattr(h2s.time, "sleep", lambda s: slept.append(s))
        self.slept = slept
        yield

    def test_spaces_out_requests(self):
        """请求之间要有间隔，否则必然 429。"""
        f = _Fetcher({f"k-{i}": {"building_name": 614} for i in range(4)})
        h2s._enrich(f, [_listing(f"k-{i}") for i in range(4)])
        # 首个请求不等，之后每个都等
        assert self.slept == [h2s._DETAIL_REQUEST_SPACING] * 3

    def test_spacing_is_meaningful(self):
        """间隔太小等于没限速；太大会把抓取拖垮。"""
        assert 0.2 <= h2s._DETAIL_REQUEST_SPACING <= 2.0

    def test_cached_listings_do_not_sleep(self):
        """稳态全是缓存命中，不该白等——否则每轮凭空多几十秒。"""
        f = _Fetcher({"k-0": {"building_name": 614}})
        h2s._enrich(f, [_listing("k-0")])
        self.slept.clear()
        h2s._enrich(f, [_listing("k-0")])
        assert self.slept == []

    def test_stops_on_rate_limit(self):
        """撞到 429 本轮收手，别继续撞墙。

        没有这个开关时，46 条里 24 条在反复撞同一堵墙——既补不上，又加重限流。
        """
        from scrapers.base import RateLimitError

        class _Limited(_Fetcher):
            def fetch_gql(self, *a, **k):
                self.calls.append(1)
                raise RateLimitError("Holland2Stay 返回 429 Too Many Requests")

        f = _Limited()
        h2s._enrich(f, [_listing(f"k-{i}") for i in range(10)])
        assert len(f.calls) == 1, f"429 后还在继续打：{len(f.calls)} 次"

    def test_rate_limit_is_reported(self, caplog):
        import logging
        from scrapers.base import RateLimitError

        class _Limited(_Fetcher):
            def fetch_gql(self, *a, **k):
                raise RateLimitError("429")

        with caplog.at_level(logging.WARNING, logger=h2s.logger.name):
            h2s._enrich(_Limited(), [_listing("k-0")])
        assert "429" in caplog.text


class TestBatchWideBudget:
    """预算与限速是**批次级**的，不是每城一份。

    2026-08-20 那次 429 只修了单城内部：``spent`` / 请求间隔 / 撞 429 收手
    全是 ``_enrich`` 的局部变量，而 ``_enrich`` **每个城市调一次**。线上
    H2S 有 19 个城市且没配 SHARD_SIZES，于是每轮有 19 份独立预算、19 次
    独立撞墙：Eindhoven 撞到 429 收手，Rotterdam 紧接着从零开始再撞一次。

    上游的限流按**出口 IP 的请求速率**算，它不关心我们内部怎么分城。所以
    预算、间隔、收手开关都必须挂在一个跨城共享的对象上。
    """

    def test_budget_is_shared_across_cities(self):
        """两个城市合起来才花掉一份预算，不是各花一份。"""
        f = _Fetcher({f"k-{i}": {"building_name": 614} for i in range(10)})
        budget = h2s._DetailBudget(4)
        h2s._enrich(f, [_listing(f"k-{i}") for i in range(5)], budget)
        h2s._enrich(f, [_listing(f"k-{i}") for i in range(5, 10)], budget)
        assert len(f.calls) == 4, (
            f"预算没有跨城共享，实际打了 {len(f.calls)} 次"
        )

    def test_rate_limit_stops_the_whole_batch_not_just_one_city(self):
        """A 城撞 429 之后，B 城不该从零开始再撞一次。"""
        from scrapers.base import RateLimitError

        class _Limited(_Fetcher):
            def fetch_gql(self, *a, **k):
                self.calls.append(1)
                raise RateLimitError("Holland2Stay 返回 429 Too Many Requests")

        f = _Limited()
        budget = h2s._DetailBudget()
        h2s._enrich(f, [_listing(f"a-{i}") for i in range(5)], budget)
        h2s._enrich(f, [_listing(f"b-{i}") for i in range(5)], budget)
        assert len(f.calls) == 1, (
            f"429 之后其它城市还在继续打：共 {len(f.calls)} 次"
        )

    def test_spacing_carries_across_cities(self, monkeypatch):
        """第二个城市的第一个请求也要等间隔——限速不能在城市边界断掉。"""
        slept: list[float] = []
        monkeypatch.setattr(h2s.time, "sleep", lambda s: slept.append(s))

        f = _Fetcher({f"k-{i}": {"building_name": 614} for i in range(4)})
        budget = h2s._DetailBudget()
        h2s._enrich(f, [_listing("k-0"), _listing("k-1")], budget)
        h2s._enrich(f, [_listing("k-2"), _listing("k-3")], budget)
        assert slept == [h2s._DETAIL_REQUEST_SPACING] * 3, (
            f"跨城的那次没等间隔: {slept}"
        )

    def test_standalone_call_still_gets_its_own_budget(self):
        """不传 budget 时退化成一次性预算，老调用方行为不变。"""
        f = _Fetcher({f"k-{i}": {"building_name": 614} for i in range(2)})
        h2s._enrich(f, [_listing("k-0")])
        h2s._enrich(f, [_listing("k-1")])
        assert len(f.calls) == 2


class TestScraperBatchBudget:
    """预算对象由 ``_begin_batch()`` 每批次建一次，同批次内所有城市共享。"""

    def test_begin_batch_creates_a_fresh_budget(self):
        s = h2s.HollandStayScraper()
        s._begin_batch()
        first = s._detail_budget
        assert isinstance(first, h2s._DetailBudget)
        s._begin_batch()
        assert s._detail_budget is not first, "新批次没有重置预算"

    def test_budget_survives_within_a_batch(self):
        """同一批次里连着抓多个城市，用的必须是同一个预算对象。"""
        s = h2s.HollandStayScraper()
        s._begin_batch()
        budget = s._detail_budget
        budget.remaining = 3
        budget.stopped = True
        assert s._detail_budget is budget


class TestBudgetIsNotLoadBearingForCorrectness:
    """预算是**流量旋钮**，不是正确性机制——把它调到 1 也不该丢数据。

    这条曾经不成立。当时 `storage.diff()` 每轮整体覆盖 features，而列表查询里
    没有 Building / Tenant，所以没轮到补齐的房源写库时会把上一轮存好的值抹掉。
    于是 `_DETAIL_BUDGET_PER_ROUND` 的注释里写着「不能设太小，原因不是性能而是
    正确性」，还配了一条 `>= 50` 的下限断言。

    v1.17.3 的粘性字段合并（``mstorage/_listings.py:_STICKY_FEATURE_KEYS``）
    把这件事接管了：抓取侧没给这几个 key ≠ 上游没有了，只是这轮没去问，写库时
    从旧值补回来。**那条正确性论证从此失效**，但注释和断言一直留着——于是同一
    个问题挂着四层机制，其中一层的存在理由已经是假的。

    这里改成钉住真正的性质：预算多小都不丢数据。数字大小只影响冷启动多快铺满。
    """

    def test_a_spent_budget_after_a_lost_cache_loses_nothing(self, tmp_path):
        """预算 0 + 缓存已丢 = 房源裸着写库。库里必须还留着上次补到的 Building。

        「缓存已丢」不是假设，是每次部署的常态：``_DETAIL_CACHE`` 是进程级的，
        容器一重建就清零。紧接着的那几轮，预算被别的城市花光 / 撞 429 收手，
        这条房源就会以「没有 Building」的形态写库。

        ⚠️ 构造这个场景必须显式清缓存。第一版测试只是「预算 1、跑两轮」，
        看着像覆盖了，实际第二轮全是缓存命中——两条房源都带着 Building 落库，
        粘性合并一次都没被调用。把粘性合并整个短路掉，那个测试照样通过。
        """
        from storage import Storage

        f = _Fetcher({"k-0": {"building_name": 614}})
        st = Storage(tmp_path / "t.db", timezone_str="UTC")
        try:
            # 1) 正常补齐一轮，Building 落库
            ls = [_listing("k-0")]
            h2s._enrich(f, ls, h2s._DetailBudget())
            st.diff(ls)

            # 2) 部署 / 重启：进程级缓存清零
            h2s._DETAIL_CACHE.clear()

            # 3) 重启后这一轮预算已被别的城市花光，这条补不到，裸着写库
            bare = [_listing("k-0")]
            h2s._enrich(f, bare, h2s._DetailBudget(0))
            assert not bare[0].features, "前提没成立：这轮本该是裸的"
            st.diff(bare)

            feats = json.loads(st.get_all_listings()[0]["features"])
            assert "Building: Philips Bedrijfsschool" in feats, (
                "楼盘被抹了——正确性又回到「预算得够大」上了"
            )
        finally:
            st.close()

    def test_a_rate_limited_round_loses_nothing_either(self, tmp_path):
        """撞 429 收手是同一个场景的另一半：这轮就是补不到。"""
        from scrapers.base import RateLimitError
        from storage import Storage

        class _Limited(_Fetcher):
            def fetch_gql(self, *a, **k):
                raise RateLimitError("429")

        f = _Fetcher({"k-0": {"building_name": 614}})
        st = Storage(tmp_path / "t.db", timezone_str="UTC")
        try:
            ls = [_listing("k-0")]
            h2s._enrich(f, ls, h2s._DetailBudget())
            st.diff(ls)

            h2s._DETAIL_CACHE.clear()
            bare = [_listing("k-0")]
            h2s._enrich(_Limited(), bare, h2s._DetailBudget())
            assert not bare[0].features
            st.diff(bare)

            feats = json.loads(st.get_all_listings()[0]["features"])
            assert "Building: Philips Bedrijfsschool" in feats
        finally:
            st.close()

    def test_correctness_is_owned_by_the_sticky_merge(self):
        """正确性归属必须明确：粘性表要覆盖补齐能产出的全部 key。

        真正的断言在 ``tests/test_sticky_features.py::test_all_enrichment_keys_are_covered``；
        这里只是从补齐这一侧再钉一次归属，免得有人回头又把它塞进预算里。
        """
        from mstorage._listings import _STICKY_FEATURE_KEYS

        produced = set(h2s._detail_features(
            {"building_name": 614, "tenant_profile": 6213,
             "neighborhood": "Strijp", "min_income": "3.5"},
            _AGGS,
        ))
        assert produced <= set(_STICKY_FEATURE_KEYS)


# ── 重启后回填缓存 ──────────────────────────────────────────────────


class TestPrimeDetailCache:
    """缓存是进程级的，重启即清零——而库里明明已经有那些值了。

    不回填的后果（2026-08-25 生产实测）：部署两分钟后的那一轮，37 条房源里
    24 条被重新问了一遍详情，第 25 条撞 429 收手，而排在后面的
    ``beukenlaan-143-093`` 是**当轮唯一的新房源**，就此少了 Building/Tenant。
    它带着残缺的 feature 直接发了通知，勾了租客条件的用户被 fail-closed 拒掉，
    补齐之后也不会补发——粘性合并救不了它，粘性只能保住已经有过的值。
    """

    def test_primed_listings_cost_no_request(self):
        h2s.prime_detail_cache({"x-1": {"Building": "The Wall"}})
        f = _Fetcher({"x-1": {"building_name": 614}})
        l = _listing("x-1")
        h2s._enrich(f, [l])

        assert f.calls == [], "库里已经有值了，不该再问一次详情"
        assert "Building: The Wall" in l.features

    def test_new_listing_still_gets_the_budget(self):
        """回填之后「不在缓存里」就等价于「这条是新的」——预算全花在它身上。"""
        h2s.prime_detail_cache({f"old-{i}": {"Building": "B"} for i in range(30)})
        f = _Fetcher({"new-1": {"building_name": 614, "tenant_profile": 6213}})
        listings = [_listing(f"old-{i}") for i in range(30)] + [_listing("new-1")]

        budget = h2s._DetailBudget()
        budget.remaining = 1          # 极端情况：预算只够一条
        h2s._enrich(f, listings, budget)

        assert [c["key"] for c in f.calls] == ["new-1"]
        assert "Tenant: student only" in listings[-1].features

    def test_returns_how_many_were_added(self):
        assert h2s.prime_detail_cache({"a": {"Building": "X"}, "b": {"Tenant": "Y"}}) == 2
        # 已经在缓存里的不重复计数，也不覆盖
        assert h2s.prime_detail_cache({"a": {"Building": "改了"}}) == 0
        assert h2s._DETAIL_CACHE["a"] == {"Building": "X"}

    def test_empty_and_junk_are_ignored(self):
        """空值不能进缓存——进了就等于宣布「这条详情查过了，没有」，永不再问。"""
        assert h2s.prime_detail_cache({}) == 0
        assert h2s.prime_detail_cache(None) == 0
        assert h2s.prime_detail_cache({"a": {}}) == 0
        assert h2s.prime_detail_cache({"a": {"Building": ""}}) == 0
        assert h2s.prime_detail_cache({"": {"Building": "X"}}) == 0
        assert h2s._DETAIL_CACHE == {}

    def test_snapshot_keys_match_what_the_detail_query_produces(self):
        """两侧对同一组 key 的认知必须一致，否则回填要么漏要么塞进无效项。

        storage 侧的 _STICKY_FEATURE_KEYS 决定快照里放什么，抓取侧的
        _detail_features 决定能补出什么。靠注释维持一致不算一致。
        """
        from mstorage._listings import _STICKY_FEATURE_KEYS

        produced = h2s._detail_features(
            {"building_name": 614, "tenant_profile": 6213,
             "neighborhood": "Strijp", "min_income": "3x"},
            _AGGS,
        )
        assert set(produced) == set(_STICKY_FEATURE_KEYS)


class TestPrimingIsActuallyWiredUp:
    """回填得在**进程启动时**真的被调用——单测自己调一遍，接线断了照样全绿。"""

    def test_bind_persistent_state_primes_the_cache(self):
        import inspect

        import monitor

        src = inspect.getsource(monitor._bind_persistent_state)
        assert "prime_detail_cache" in src, (
            "_bind_persistent_state 必须回填详情缓存，否则每次部署后头几轮"
            "都会把已补齐的房源重问一遍，撞 429 挤掉当轮的新房源")
        assert "detail_feature_snapshot" in src

    def test_priming_failure_does_not_stop_startup(self, tmp_path, monkeypatch):
        """回填是省事的，不是必需的——库读不出来也得照常启动。"""
        import monitor
        from mstorage import Storage

        st = Storage(tmp_path / "t.db")
        monkeypatch.setattr(
            st, "detail_feature_snapshot",
            lambda source: (_ for _ in ()).throw(RuntimeError("库炸了")),
        )
        try:
            monitor._bind_persistent_state(st)   # 不抛就算过
        finally:
            st.close()
