"""
API v1 公共统计端点（guest 可访问）
=====================================

设计原则
--------
- 这里的数据**不包含任何用户隔离信息**：只有"全库"级别的聚合统计。
  guest 看的图表与 admin 看的完全一致，不会泄漏单房源的 listing_id /
  细节字段。
- 仍走 ``@bearer_optional``：如果客户端带了 token 也接受（便于统一
  日志/限流），但不要求。
- 复用 ChartOps 的现成方法，零业务逻辑重复。
"""

from __future__ import annotations

from flask import Blueprint, request

from app import api_auth, api_errors as _err
from app.db import storage


def _summary():
    """GET /stats/public/summary —— 首页几个总览数字。"""
    st = storage()
    try:
        data = {
            "total": st.count_all(),
            "new_24h": st.count_new_since(hours=24),
            "new_7d": st.count_new_since(hours=24 * 7),
            "changes_24h": st.count_changes_since(hours=24),
            "last_scrape": st.get_meta("last_scrape_at", default=""),
        }
    finally:
        st.close()
    return _err.ok(data)


# 公开图表白名单——只放纯聚合、不涉及单房源的指标。
# city_dist / status_dist 等都是 COUNT(*) GROUP BY，没有 listing_id 泄漏。
_PUBLIC_CHARTS = {
    "daily_new":     lambda st, days: st.chart_daily_new(days=days),
    "daily_changes": lambda st, days: st.chart_daily_changes(days=days),
    "city_dist":     lambda st, _:    st.chart_city_dist(),
    "status_dist":   lambda st, _:    st.chart_status_dist(),
    "price_dist":    lambda st, _:    st.chart_price_dist(),
    "hourly_dist":   lambda st, _:    st.chart_hourly_dist(),
    "tenant_dist":   lambda st, _:    st.chart_tenant_dist(),
    "contract_dist": lambda st, _:    st.chart_contract_dist(),
    "type_dist":     lambda st, _:    st.chart_type_dist(),
    "energy_dist":   lambda st, _:    st.chart_energy_dist(),
    "area_dist":     lambda st, _:    st.chart_area_dist(),
    "floor_dist":    lambda st, _:    st.chart_floor_dist(),
}


def _chart(key: str):
    """GET /stats/public/charts/<key>?days=30"""
    fn = _PUBLIC_CHARTS.get(key)
    if fn is None:
        return _err.err_not_found(f"未知图表 {key!r}")
    try:
        days = int(request.args.get("days", 30))
    except (TypeError, ValueError):
        days = 30
    days = max(1, min(days, 365))
    st = storage()
    try:
        data = fn(st, days)
    finally:
        st.close()
    return _err.ok({"key": key, "days": days, "data": data})


def _charts_index():
    """GET /stats/public/charts —— 列出所有可用图表 key。"""
    return _err.ok({"charts": sorted(_PUBLIC_CHARTS.keys())})


def register(bp: Blueprint) -> None:
    bp.add_url_rule(
        "/stats/public/summary",
        endpoint="stats_public_summary",
        view_func=api_auth.bearer_optional(_summary),
        methods=["GET"],
    )
    bp.add_url_rule(
        "/stats/public/charts",
        endpoint="stats_public_charts_index",
        view_func=api_auth.bearer_optional(_charts_index),
        methods=["GET"],
    )
    bp.add_url_rule(
        "/stats/public/charts/<string:key>",
        endpoint="stats_public_chart",
        view_func=api_auth.bearer_optional(_chart),
        methods=["GET"],
    )
