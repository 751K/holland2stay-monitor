"""
API v1 当前用户专属端点
==========================

- GET /api/v1/me/summary
    "我"的概览：匹配房源数、最近 24h 新增、未读通知数。
    admin role 仍然能调，但返回的是无 filter 的全库数字。

- GET /api/v1/me/filter
    返回当前 user 的 listing_filter（user only）。
    Phase 5 后会加 PUT 用于自助修改。

设计原则
--------
这里只放 "我"-视角的端点，admin 用 ``/api/v1/listings`` 等通用端点
就能看到全量数据。
"""

from __future__ import annotations

from flask import Blueprint

from app import api_auth, api_errors as _err

from ._helpers import (
    apply_user_filter,
    get_current_user,
    serialize_filter,
    storage_ctx,
)


def _summary():
    role = api_auth.current_role()
    user = get_current_user() if role == "user" else None
    if role == "user" and user is None:
        return _err.err_unauthorized("用户已被删除")

    with storage_ctx() as st:
        last_scrape = st.get_meta("last_scrape_at", default="")
        # admin：全库；user：先全库取 500 条，再 listing_filter 过滤算量
        # （现实数据 ≤ 千级别，开销可忽略；超大场景以后再 SQL 侧实现）
        if role == "user" and user is not None:
            recent_24h = st.get_all_listings(limit=2000)
            new_24h = st.count_new_since(hours=24)
            total_in_db = st.count_all()
            matched = apply_user_filter(recent_24h, user)
            data = {
                "role": role,
                "total_in_db": total_in_db,
                "new_24h_total": new_24h,
                "matched_total": len(matched),
                "matched_available": sum(
                    1 for r in matched if "available" in (r.get("status") or "").lower()
                ),
                "last_scrape": last_scrape,
                "filter_active": not user.listing_filter.is_empty(),
            }
        else:
            data = {
                "role": role,
                "total_in_db": st.count_all(),
                "new_24h_total": st.count_new_since(hours=24),
                "matched_total": st.count_all(),
                "matched_available": None,
                "last_scrape": last_scrape,
                "filter_active": False,
            }
    return _err.ok(data)


def _filter():
    """role=user 时返回 listing_filter；admin/其他返回空 filter（一致 shape）。"""
    role = api_auth.current_role()
    user = get_current_user() if role == "user" else None
    if role == "user" and user is None:
        return _err.err_unauthorized("用户已被删除")
    return _err.ok({
        "role": role,
        "filter": serialize_filter(user),
        "is_empty": user.listing_filter.is_empty() if user else True,
    })


def register(bp: Blueprint) -> None:
    bp.add_url_rule(
        "/me/summary",
        endpoint="me_summary",
        view_func=api_auth.bearer_required(("admin", "user"))(_summary),
        methods=["GET"],
    )
    bp.add_url_rule(
        "/me/filter",
        endpoint="me_filter",
        view_func=api_auth.bearer_required(("admin", "user"))(_filter),
        methods=["GET"],
    )
