"""
路由：统计图表

挂载的 endpoint
- GET /stats       → stats（页面）
- GET /api/charts  → api_charts（全站口径的 JSON，App 等外部调用）
- GET /stats/data  → stats_data（统计页用，按当前收件箱范围统计）
"""
from __future__ import annotations

from flask import Flask, jsonify, render_template, request

from app.auth import api_login_required, browse_required
from app.services.stats_service import (
    DEFAULT_STATS_DAYS,
    charts_payload,
    normalize_days,
)


@browse_required
def stats() -> str:
    from app.i18n import get_lang
    from app.services.inbox_service import build_inbox_scope, workspace_url
    from app.services.web_identity import current_listing_user

    default_days = DEFAULT_STATS_DAYS
    # 侧栏与收件箱同一份（范围、视图计数、平台计数），需要同样的范围上下文；
    # 侧栏里的链接默认回到列表页，而不是留在统计页
    scope = build_inbox_scope(current_listing_user(), request.args, zh=get_lang() == "zh")
    for key in ("visible_rows", "read_ids", "match_ids"):
        scope.pop(key)

    def inbox_url(endpoint=None, **changes):
        return workspace_url(endpoint or "index", **changes)

    return render_template(
        "stats.html",
        **scope,
        inbox_url=inbox_url,
        default_days=default_days,
    )


@api_login_required
def api_charts():
    """所有图表数据的 JSON API，供前端 Chart.js 调用。"""
    days = normalize_days(request.args.get("days", DEFAULT_STATS_DAYS))
    return jsonify(charts_payload(days=days))


@browse_required
def stats_data():
    """统计页的数据：与 /api/charts 同形，但只算当前收件箱范围内的房源
    （我的匹配，或带临时筛选的全部房源）。/api/charts 保持全站口径不变。"""
    from app.services.inbox_service import scope_listing_ids
    from app.services.web_identity import current_listing_user

    days = normalize_days(request.args.get("days", DEFAULT_STATS_DAYS))
    ids = scope_listing_ids(current_listing_user(), request.args)
    return jsonify(charts_payload(days=days, ids=ids))


def register(app: Flask) -> None:
    app.add_url_rule("/stats",      endpoint="stats",      view_func=stats,      methods=["GET"])
    app.add_url_rule("/api/charts", endpoint="api_charts", view_func=api_charts, methods=["GET"])
    app.add_url_rule("/stats/data", endpoint="stats_data", view_func=stats_data, methods=["GET"])
