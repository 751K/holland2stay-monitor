"""
路由：仪表盘首页 + 房源列表

挂载的 endpoint
- GET / → index
- GET /listings → listings
"""
from __future__ import annotations

import logging

from flask import Flask, jsonify, render_template, request

logger = logging.getLogger(__name__)

from app.auth import api_login_required, login_required
from app.db import storage
from app.process_ctrl import monitor_pid
from app.services.dashboard_service import dashboard_metrics
from app.services.onboarding_service import delivery_state
from app.i18n import get_lang
from app.services.listing_service import (
    get_filter_options,
    normalize_listing_rows,
    query_listing_rows,
)


def _onboarding_state():
    """当前登录用户的投递状态；不适用时返回 None（模板据此整块跳过）。

    只对 user 角色算：admin 和 guest 没有 UserConfig 行，guest 更是连账号
    都没有。算不出来时返回 None 而不是抛——引导缺一块不该让首页 500。
    """
    from app.auth import current_user_id, is_user

    if not is_user():
        return None
    uid = current_user_id()
    if not uid:
        return None
    try:
        from users import load_users

        user = next((u for u in load_users() if u.id == uid), None)
        if user is None:
            return None
        st = storage()
        try:
            return delivery_state(st, user, get_lang())
        finally:
            st.close()
    except Exception:
        logger.warning("计算引导状态失败 user_id=%s", uid, exc_info=True)
        return None


@login_required
def index() -> str:
    city_filter = request.args.get("city", "")
    st = storage()
    try:
        # get_distinct_cities() 走 SELECT DISTINCT city，比拉 2000 行再 set
        # 推 SQL 端做去重，且没有 LIMIT 截断导致老城市丢失的正确性 bug。
        all_cities = st.get_distinct_cities()
        status_counts = st.count_by_status(city=city_filter or None)
        stats = dashboard_metrics(st, city=city_filter or None, lang=get_lang())
        stats["book_count"] = status_counts.get("available to book", 0)
        stats["lottery_count"] = status_counts.get("available in lottery", 0)
        recent  = normalize_listing_rows(st.get_all_listings(city=city_filter or None, limit=15))
        changes = st.get_recent_changes(hours=48, city=city_filter or None)
    finally:
        st.close()
    pid = monitor_pid()

    # 覆盖范围横幅的城市名。取自当前生效的配置而非写死——这些值存在 app_settings
    # 里，随时能从「设置」页改；写死的横幅迟早和实际不符，且不会有任何地方报错。
    # 分隔符按语言给：中文用顿号，英文用逗号加空格。
    lang = get_lang()
    try:
        from config import load_config

        cities = load_config().monitored_city_names()
    except Exception:
        # 配置读不出来时不显示横幅，而不是让整个首页 500
        logger.warning("读取监控城市失败，本次不显示覆盖范围横幅", exc_info=True)
        cities = []
    monitored_cities_text = ("、" if lang == "zh" else ", ").join(cities)
    # 支持邮箱同样取配置（SUPPORT_EMAIL）：写死会和支持页显示的不一致，
    # 而那正是 App Store 登记的 Support URL 上写的地址。
    from support_text import CONTACT_EMAIL as support_email

    return render_template(
        "index.html",
        monitored_cities_text=monitored_cities_text,
        support_email=support_email,
        onb=_onboarding_state(),
        stats=stats,
        recent=recent,
        changes=changes,
        monitor_running=pid is not None,
        city_filter=city_filter,
        all_cities=all_cities,
    )


@api_login_required
def api_dashboard_summary():
    city_filter = request.args.get("city", "")
    st = storage()
    try:
        stats = dashboard_metrics(st, city=city_filter or None, lang=get_lang())
        return jsonify({"ok": True, "city": city_filter, "summary": stats})
    finally:
        st.close()


@login_required
def listings() -> str:
    from models import parse_float

    status_filter  = request.args.get("status", "")
    name_query     = request.args.get("q", "")
    city_filters   = request.args.getlist("city")  # 多选
    source_filters = request.args.getlist("source")  # 多选
    type_filters   = request.args.getlist("type")  # 多选：房型（Studio / 1-Bedroom / Loft）
    occupancy_filters = request.args.getlist("occupancy")  # 多选：允许入住人数
    max_rent_str   = request.args.get("max_rent", "")
    min_area_str   = request.args.get("min_area", "")
    contract_filter = request.args.get("contract", "")
    tenant_filters = request.args.getlist("tenant")  # 多选
    energy_filter  = request.args.get("energy", "")  # 单选：最低可接受等级
    finishing_filters = request.args.getlist("finishing")  # 多选：装修程度
    max_rent = parse_float(max_rent_str) if max_rent_str.strip() else None
    min_area = parse_float(min_area_str) if min_area_str.strip() else None
    rows = query_listing_rows(
        status=status_filter or None,
        search=name_query or None,
        cities=city_filters,
        sources=source_filters,
        types=type_filters,
        max_rent=max_rent,
        min_area=min_area,
        contract=contract_filter or None,
        tenants=tenant_filters,
        occupancies=occupancy_filters,
        energy=energy_filter or None,
        finishing=finishing_filters or None,
        limit=500,
    )
    options = get_filter_options()
    return render_template(
        "listings.html",
        listings=rows, statuses=options["statuses"],
        status_filter=status_filter, search=name_query, city_filters=city_filters,
        source_filters=source_filters,
        type_filters=type_filters,
        occupancy_filters=occupancy_filters,
        cities=options["cities"],
        sources=options["sources"],
        types=options["types"],
        occupancies=options["occupancies"],
        max_rent=max_rent_str, min_area=min_area_str,
        contract_filter=contract_filter, tenant_filters=tenant_filters,
        energy_filter=energy_filter, finishing_filters=finishing_filters,
        contracts=options["contracts"], tenants=options["tenants"],
        energies=options["energies"], finishings=options["finishings"],
    )


def register(app: Flask) -> None:
    app.add_url_rule("/",         endpoint="index",    view_func=index,    methods=["GET"])
    app.add_url_rule("/api/dashboard/summary", endpoint="api_dashboard_summary", view_func=api_dashboard_summary, methods=["GET"])
    app.add_url_rule("/listings", endpoint="listings", view_func=listings, methods=["GET"])
