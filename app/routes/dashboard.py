"""
路由：统计摘要 API

挂载的 endpoint
- GET /api/dashboard/summary → api_dashboard_summary

管理后台原来的 /dashboard 首页和 /listings 旧列表页已删除：房源看收件箱，
管理入口是用户列表。
"""
from __future__ import annotations

import logging

from flask import Flask, jsonify, request

logger = logging.getLogger(__name__)

from app.auth import api_login_required
from app.db import storage
from app.services.dashboard_service import dashboard_metrics
from app.i18n import get_lang


@api_login_required
def api_dashboard_summary():
    city_filter = request.args.get("city", "")
    st = storage()
    try:
        stats = dashboard_metrics(st, city=city_filter or None, lang=get_lang())
        return jsonify({"ok": True, "city": city_filter, "summary": stats})
    finally:
        st.close()


def register(app: Flask) -> None:
    app.add_url_rule("/api/dashboard/summary", endpoint="api_dashboard_summary", view_func=api_dashboard_summary, methods=["GET"])
