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

from ._helpers import apply_user_filter, get_current_user, storage_ctx


def _calendar():
    role = api_auth.current_role()
    user = get_current_user() if role == "user" else None
    if role == "user" and user is None:
        return _err.err_unauthorized("用户已被删除")

    with storage_ctx() as st:
        listings = st.get_calendar_listings()
        # get_calendar_listings 不含 features JSON 串；为了应用 listing_filter
        # 需重新从 listings 表把原始行拉回来，与 map 端点同样的做法。
        if role == "user" and user is not None and not user.listing_filter.is_empty():
            ids = [l["id"] for l in listings]
            if ids:
                placeholders = ",".join("?" * len(ids))
                raw_rows = st.conn.execute(
                    f"SELECT * FROM listings WHERE id IN ({placeholders})",
                    ids,
                ).fetchall()
                kept = {r["id"] for r in apply_user_filter(
                    [dict(r) for r in raw_rows], user,
                )}
                listings = [l for l in listings if l["id"] in kept]
    return _err.ok({"listings": listings})


def register(bp: Blueprint) -> None:
    bp.add_url_rule(
        "/calendar",
        endpoint="calendar_list",
        view_func=api_auth.bearer_required(("admin", "user"))(_calendar),
        methods=["GET"],
    )
