"""
API v1 地图端点
================

GET /api/v1/map
    返回所有**已缓存坐标**的房源（不触发外部 Photon 请求）。
    user 视角应用 listing_filter；admin 视角全量。
"""

from __future__ import annotations

from flask import Blueprint

from app import api_auth, api_errors as _err

from ._helpers import apply_user_filter, get_current_user, storage_ctx


def _map():
    role = api_auth.current_role()
    user = get_current_user() if role == "user" else None
    if role == "user" and user is None:
        return _err.err_unauthorized("用户已被删除")

    results: list[dict] = []
    uncached = 0
    with storage_ctx() as st:
        listings = st.get_map_listings()
        # user 模式：先按 listing_filter 收窄；地图 dict 含 id/name/status/etc.，
        # apply_user_filter 会把 row 当 listings 表行处理（features 字段对应）。
        # get_map_listings 返回的 row 没有 features JSON 串——它已经被 ChartOps
        # 拆成 neighborhood/area 等 plain 字段。所以这里要重新从 listings 拿原始 row
        # 才能跑 listing_filter。简单做法：直接按 id set 去 listings 表过滤。
        if role == "user" and user is not None and not user.listing_filter.is_empty():
            ids = {l["id"] for l in listings}
            if ids:
                placeholders = ",".join("?" * len(ids))
                raw_rows = st.conn.execute(
                    f"SELECT * FROM listings WHERE id IN ({placeholders})",
                    list(ids),
                ).fetchall()
                kept = {r["id"] for r in apply_user_filter(
                    [dict(r) for r in raw_rows], user,
                )}
                listings = [l for l in listings if l["id"] in kept]

        for l in listings:
            cached = st.get_cached_coords(l["address"])
            if cached:
                lat, lng = cached
                results.append({**l, "lat": lat, "lng": lng})
            else:
                uncached += 1

    return _err.ok({"listings": results, "uncached": uncached})


def register(bp: Blueprint) -> None:
    bp.add_url_rule(
        "/map",
        endpoint="map_list",
        view_func=api_auth.bearer_required(("admin", "user"))(_map),
        methods=["GET"],
    )
