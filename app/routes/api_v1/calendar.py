"""
API v1 日历端点
================

GET /api/v1/calendar
    返回所有有 available_from 入住日期的房源。
    user 视角应用 listing_filter；admin 全量。

返回数据与 ``/api/calendar`` (Web) 一致：id/name/status/price_raw/
available_from/url/city/building。
"""

from __future__ import annotations

from flask import Blueprint

from app import api_auth, api_errors as _err
from app.services.listing_service import get_calendar_payload

from ._helpers import get_current_user


def _calendar():
    role = api_auth.current_role()
    user = get_current_user() if role == "user" else None
    if role == "user" and user is None:
        return _err.err_unauthorized("用户已被删除")

    return _err.ok(get_calendar_payload(user if role == "user" else None))


def register(bp: Blueprint) -> None:
    bp.add_url_rule(
        "/calendar",
        endpoint="calendar_list",
        view_func=api_auth.bearer_optional(_calendar),
        methods=["GET"],
    )
