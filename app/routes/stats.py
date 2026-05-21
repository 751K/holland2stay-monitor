"""
路由：统计图表

挂载的 endpoint
- GET /stats       → stats（页面）
- GET /api/charts  → api_charts（Chart.js JSON 数据源）
"""
from __future__ import annotations

from flask import Flask, jsonify, render_template, request

from app.auth import api_login_required, login_required
from app.services.stats_service import (
    DEFAULT_STATS_DAYS,
    charts_payload,
    normalize_days,
    stats_summary,
)


@login_required
def stats() -> str:
    default_days = DEFAULT_STATS_DAYS
    summary = stats_summary(days=default_days)
    return render_template(
        "stats.html",
        total=summary["total"],
        new_24h=summary["new_24h"],
        new_range=summary["new_range"],
        changes_range=summary["changes_range"],
        default_days=default_days,
    )


@api_login_required
def api_charts():
    """所有图表数据的 JSON API，供前端 Chart.js 调用。"""
    days = normalize_days(request.args.get("days", DEFAULT_STATS_DAYS))
    return jsonify(charts_payload(days=days))


def register(app: Flask) -> None:
    app.add_url_rule("/stats",      endpoint="stats",      view_func=stats,      methods=["GET"])
    app.add_url_rule("/api/charts", endpoint="api_charts", view_func=api_charts, methods=["GET"])
