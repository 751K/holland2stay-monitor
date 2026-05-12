"""
路由：统计图表

挂载的 endpoint
- GET /stats       → stats（页面）
- GET /api/charts  → api_charts（Chart.js JSON 数据源）
"""
from __future__ import annotations

from flask import Flask, jsonify, render_template, request

from app.auth import api_login_required, login_required
from app.db import storage


@login_required
def stats() -> str:
    st = storage()
    try:
        total       = st.count_all()
        new_24h     = st.count_new_since(hours=24)
        new_7d      = st.count_new_since(hours=24 * 7)
        changes_24h = st.count_changes_since(hours=24)
    finally:
        st.close()
    return render_template(
        "stats.html",
        total=total, new_24h=new_24h, new_7d=new_7d, changes_24h=changes_24h,
    )


@api_login_required
def api_charts():
    """所有图表数据的 JSON API，供前端 Chart.js 调用。"""
    try:
        days = int(request.args.get("days", 30))
    except (TypeError, ValueError):
        days = 30
    days = max(1, min(days, 365))  # 限制在 [1, 365]，防止超大查询
    st = storage()
    try:
        data = {
            "daily_new":     st.chart_daily_new(days=days),
            "daily_changes": st.chart_daily_changes(days=days),
            "city_dist":     st.chart_city_dist(),
            "status_dist":   st.chart_status_dist(),
            "price_dist":    st.chart_price_dist(),
            "hourly_dist":   st.chart_hourly_dist(),
        }
    finally:
        st.close()
    return jsonify(data)


def register(app: Flask) -> None:
    app.add_url_rule("/stats",      endpoint="stats",      view_func=stats,      methods=["GET"])
    app.add_url_rule("/api/charts", endpoint="api_charts", view_func=api_charts, methods=["GET"])
