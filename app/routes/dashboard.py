"""
路由：仪表盘首页 + 房源列表

挂载的 endpoint
- GET / → index
- GET /listings → listings
"""
from __future__ import annotations

import logging

from flask import Flask, render_template, request

logger = logging.getLogger(__name__)

from app.auth import login_required
from app.db import storage
from app.process_ctrl import monitor_pid
from app.services.listing_service import get_filter_options, query_listing_rows


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
    from models import parse_float

    status_filter  = request.args.get("status", "")
    name_query     = request.args.get("q", "")
    city_filters   = request.args.getlist("city")  # 多选
    max_rent_str   = request.args.get("max_rent", "")
    min_area_str   = request.args.get("min_area", "")
    contract_filter = request.args.get("contract", "")
    tenant_filters = request.args.getlist("tenant")  # 多选
    energy_filter  = request.args.get("energy", "")  # 单选：最低可接受等级
    finishing_filter = request.args.get("finishing", "")
    max_rent = parse_float(max_rent_str) if max_rent_str.strip() else None
    min_area = parse_float(min_area_str) if min_area_str.strip() else None
    rows = query_listing_rows(
        status=status_filter or None,
        search=name_query or None,
        cities=city_filters,
        max_rent=max_rent,
        min_area=min_area,
        contract=contract_filter or None,
        tenants=tenant_filters,
        energy=energy_filter or None,
        finishing=finishing_filter or None,
        limit=500,
    )
    options = get_filter_options()
    return render_template(
        "listings.html",
        listings=rows, statuses=options["statuses"],
        status_filter=status_filter, search=name_query, city_filters=city_filters,
        cities=options["cities"],
        max_rent=max_rent_str, min_area=min_area_str,
        contract_filter=contract_filter, tenant_filters=tenant_filters,
        energy_filter=energy_filter, finishing_filter=finishing_filter,
        contracts=options["contracts"], tenants=options["tenants"],
        energies=options["energies"], finishings=options["finishings"],
    )


def register(app: Flask) -> None:
    app.add_url_rule("/",         endpoint="index",    view_func=index,    methods=["GET"])
    app.add_url_rule("/listings", endpoint="listings", view_func=listings, methods=["GET"])
