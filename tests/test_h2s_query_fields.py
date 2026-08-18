"""H2S 的 GraphQL 查询必须与站点原文逐字段一致。

**这个文件的前提在 2026-08-18 被反转过一次。**

原先守的是「只请求 _to_listing 真正读取的字段」——为省流量而裁剪查询，多请求
一个字段就失败。当时实测 media_gallery 一项就占响应体 70%，裁剪后 92 → 26 MB/天。

那天 H2S 上线了 GraphQL operation 白名单：不在名单里的查询一律
``403 {"code":"operation_not_allowed"}``。而我们那份裁剪版恰恰不在名单里，于是
H2S 抓取全量中断。实测判据：

    站点原文                            200
    删掉 image_manager 块                403
    加 tenant_profile_restrictions       403
    加 available_startdate               403
    只改空格                             200   ← 空白不敏感，字段集敏感

所以现在守的是**反过来的约束**：查询是照抄品，一个字段都不能增删。省流量改走
「查什么」（scrapers.holland2stay 的分层抓取，那些走 variables，白名单不管）。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_GQL = _ROOT / "h2s_gql.py"
_SCRAPER = _ROOT / "scrapers" / "holland2stay.py"


def _items_fields() -> set[str]:
    """抽出查询里 ``items { ... }`` 的顶层字段名。"""
    import h2s_gql

    src = h2s_gql.GQL_QUERY
    start = src.index("items {") + len("items {")
    depth, i = 1, start
    while depth:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    body = src[start:i - 1]

    fields: set[str] = set()
    depth = 0
    for line in body.splitlines():
        tok = line.strip()
        if not tok:
            continue
        if depth == 0:
            m = re.match(r"^([a-z_][a-z0-9_]*)", tok)
            if m:
                fields.add(m.group(1))
        depth += tok.count("{") - tok.count("}")
    return fields


class TestQueryIsVerbatim:
    """查询是照抄品，不是设计品。"""

    #: 2026-08-18 从线上明文抄下来时，items 选择集里的字段。
    #: 这份清单**不是需求**，是上游的既成事实。它变了只能重新照抄，不能自行增删。
    EXPECTED = {
        "name", "sku", "city", "url_key", "available_to_book",
        "next_contract_startdate", "current_lottery_subscribers", "finishing",
        "living_area", "no_of_rooms", "offer_text_two", "offer_text",
        "maximum_number_of_persons", "type_of_contract", "price_analysis_text",
        "allowance_price", "floor", "basic_rent", "price_range", "energy_label",
        "minimum_stay", "media_gallery", "image_manager", "__typename",
    }

    def test_field_set_matches_the_allowlisted_document(self):
        got = _items_fields()
        added = sorted(got - self.EXPECTED)
        removed = sorted(self.EXPECTED - got)
        assert not added, (
            f"给白名单查询加了字段: {added}。加一个就会全量 403 "
            f"operation_not_allowed，H2S 直接停摆。"
        )
        assert not removed, (
            f"从白名单查询删了字段: {removed}。删一个同样 403——这正是 "
            f"2026-08-18 中断的直接原因。要省流量请改分层抓取，别动字段。"
        )

    def test_operation_name_is_declared(self):
        """缺 operationName 同样 403，实测过。"""
        import h2s_gql
        assert h2s_gql.OPERATION_NAME == "GetCategories"

    def test_scraper_does_not_define_its_own_query(self):
        """查询只有一处定义。散成两份必然有一份忘了跟着照抄。"""
        src = _SCRAPER.read_text(encoding="utf-8")
        assert "query GetCategories(" not in src, (
            "scrapers/holland2stay.py 里又出现了查询定义，"
            "它应当只从 h2s_gql 导入"
        )

    def test_scraper_sends_the_operation_name(self):
        """调用点必须带 operation_name，否则一律 403。"""
        tree = ast.parse(_SCRAPER.read_text(encoding="utf-8"))
        calls = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "fetch_gql"
        ]
        assert calls, "找不到 fetch_gql 调用，这条守卫已失效"
        for c in calls:
            kw = {k.arg for k in c.keywords}
            assert "operation_name" in kw, (
                "有一处 fetch_gql 没传 operation_name，该请求会 403"
            )


class TestFieldsWeLost:
    """照抄的代价：三个我们原本在用的字段不在名单里。"""

    @pytest.mark.parametrize("field", [
        "building_name", "available_startdate", "tenant_profile_restrictions",
    ])
    def test_absent_field_stays_absent(self, field):
        """别再把它们加回查询——加了就是全量 403。

        它们的替代来源见 docs/H2S.md §5.2：building_name 与
        tenant_profile_restrictions 可经 aggregations / 筛选条件取回，
        available_startdate 只能从 SSR 页面解析。
        """
        assert field not in _items_fields()

    def test_to_listing_tolerates_their_absence(self):
        """字段缺失时必须优雅降级，不能抛。"""
        from scrapers.holland2stay import _to_listing

        item = {
            "url_key": "x-1", "sku": "r-x-1", "available_to_book": 6203,
            "basic_rent": 1200, "living_area": "40", "energy_label": "A",
            "no_of_rooms": "6137", "floor": "6061", "finishing": 6261,
            "maximum_number_of_persons": 23, "type_of_contract": 21,
            "next_contract_startdate": "2026-09-01 00:00:00",
        }
        listing = _to_listing(item, "Eindhoven", {})
        assert listing is not None, "缺三个字段就返回 None，会把整城房源丢光"
        assert listing.id == "x-1"
        assert listing.available_from is None
        assert not [f for f in listing.features if f.startswith("Tenant:")]
