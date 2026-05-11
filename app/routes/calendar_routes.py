"""
路由：入住日历

挂载的 endpoint
- GET /calendar     → calendar
- GET /api/calendar → api_calendar

模块名加 _routes 后缀，避免与标准库 ``calendar`` 模块同名冲突。
"""
from __future__ import annotations

from flask import Flask, jsonify, render_template

from app.auth import api_login_required, login_required
from app.db import storage


@login_required
def calendar() -> str:
    return render_template("calendar.html")


@api_login_required
def api_calendar():
    """返回所有有入住日期的房源，供日历前端渲染。"""
    st = storage()
    try:
        listings = st.get_calendar_listings()
    finally:
        st.close()
    return jsonify({"listings": listings})


def register(app: Flask) -> None:
    app.add_url_rule("/calendar",     endpoint="calendar",     view_func=calendar,     methods=["GET"])
    app.add_url_rule("/api/calendar", endpoint="api_calendar", view_func=api_calendar, methods=["GET"])
