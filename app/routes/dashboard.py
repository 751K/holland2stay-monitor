"""
路由：仪表盘首页 + 房源列表

挂载的 endpoint
- GET / → index
- GET /listings → listings
"""
from __future__ import annotations

import json as _json

from flask import Flask, render_template, request

from app.auth import login_required
from app.db import storage
from app.process_ctrl import monitor_pid


def _feature_contains(row: dict, category: str, value: str) -> bool:
    """检查房源 features JSON 中指定分类是否包含某值（子串匹配，大小写不敏感）。"""
    feats = _json.loads(row.get("features", "[]"))
    for f in feats:
        if f.startswith(f"{category}: ") and value.lower() in f.lower():
            return True
    return False


@login_required
def index() -> str:
    city_filter = request.args.get("city", "")
    st = storage()
    try:
        # get_distinct_cities() 走 SELECT DISTINCT city，比拉 2000 行再 set
        # 推 SQL 端做去重，且没有 LIMIT 截断导致老城市丢失的正确性 bug。
        all_cities = st.get_distinct_cities()
        last_scrape = st.get_meta("last_scrape_at")
        stats = {
            "total":       st.count_all(city=city_filter or None),
            "new_24h":     st.count_new_since(hours=24, city=city_filter or None),
            "changes_24h": st.count_changes_since(hours=24, city=city_filter or None),
            "last_scrape": last_scrape,
            "last_count":  st.get_meta("last_scrape_count"),
        }
        recent  = st.get_all_listings(city=city_filter or None, limit=15)
        changes = st.get_recent_changes(hours=48, city=city_filter or None)
    finally:
        st.close()
    return render_template(
        "index.html",
        stats=stats,
        recent=recent,
        changes=changes,
        monitor_running=monitor_pid() is not None,
        city_filter=city_filter,
        all_cities=all_cities,
    )


@login_required
def listings() -> str:
    from models import parse_features_list, parse_float

    status_filter  = request.args.get("status", "")
    name_query     = request.args.get("q", "")
    city_filters   = request.args.getlist("city")  # 多选
    max_rent_str   = request.args.get("max_rent", "")
    min_area_str   = request.args.get("min_area", "")
    contract_filter = request.args.get("contract", "")
    tenant_filters = request.args.getlist("tenant")  # 多选
    max_rent = parse_float(max_rent_str) if max_rent_str.strip() else None
    min_area = parse_float(min_area_str) if min_area_str.strip() else None
    st = storage()
    try:
        # 单城市走 SQL 过滤，多城市或不选走 Python 过滤
        sql_city = city_filters[0] if len(city_filters) == 1 else None
        rows = st.get_all_listings(
            status=status_filter or None,
            search=name_query or None,
            city=sql_city,
            limit=500,
        )
        statuses   = st.get_distinct_statuses()
        city_list  = st.get_distinct_cities()
        contracts  = st.get_feature_values("Contract")
        tenants    = st.get_feature_values("Tenant")
    finally:
        st.close()
    # Python 端过滤（数据量小，无需 SQL 复杂度）
    if len(city_filters) > 1:
        cf_lower = {c.lower() for c in city_filters}
        rows = [r for r in rows if (r.get("city") or "").lower() in cf_lower]
    if max_rent is not None:
        rows = [r for r in rows if (pv := parse_float(r.get("price_raw", ""))) is not None and pv <= max_rent]
    if min_area is not None:
        def _get_area(r):
            fm = parse_features_list(_json.loads(r.get("features", "[]")))
            return parse_float(fm.get("area", ""))
        rows = [r for r in rows if (a := _get_area(r)) is not None and a >= min_area]
    # 合同类型 / 租客要求：子串匹配（与 ListingFilter 一致）
    if contract_filter:
        rows = [r for r in rows if _feature_contains(r, "Contract", contract_filter)]
    if tenant_filters:
        rows = [r for r in rows if any(_feature_contains(r, "Tenant", t) for t in tenant_filters)]
    return render_template(
        "listings.html",
        listings=rows, statuses=statuses,
        status_filter=status_filter, search=name_query, city_filters=city_filters,
        cities=city_list,
        max_rent=max_rent_str, min_area=min_area_str,
        contract_filter=contract_filter, tenant_filters=tenant_filters,
        contracts=contracts, tenants=tenants,
    )


def register(app: Flask) -> None:
    app.add_url_rule("/",         endpoint="index",    view_func=index,    methods=["GET"])
    app.add_url_rule("/listings", endpoint="listings", view_func=listings, methods=["GET"])
