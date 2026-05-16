"""
API v1 房源端点
================

- GET /api/v1/listings            房源列表（admin: 全量 / user: 应用 listing_filter）
- GET /api/v1/listings/<id>       单条详情（user 拿不到 filter 之外的房源 → 404）

查询参数
--------
- status        房源状态精确匹配（"Available to book" 等）
- city          单城市 SQL 过滤（兼容旧版；优先用 cities）
- cities        多城市，逗号分隔（如 "Eindhoven,Amsterdam"）
- q             名称模糊搜索
- types         房型过滤，逗号分隔（如 "Studio,Apartment"）
- contract      合同类型过滤（子串匹配，大小写不敏感）
- energy        最低能耗等级（如 "B" → 匹配 A+++..B）
- limit         分页 1-500，默认 100
- offset        偏移 ≥0，默认 0

返回壳
------
{
  "ok": true,
  "data": {
    "items":  [房源 ...],
    "total":  当前过滤条件下的总数（含分页之外）,
    "limit":  ...,
    "offset": ...,
  }
}
"""

from __future__ import annotations

import json

from flask import Blueprint, request

from app import api_auth, api_errors as _err

from ._helpers import (
    apply_user_filter,
    get_current_user,
    serialize_listing,
    storage_ctx,
)


# ── helper: feature 子串匹配 ──────────────────────────────────────────

def _safe_features(row: dict) -> list[str]:
    raw = row.get("features", "[]") or "[]"
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []


def _feature_contains(row: dict, category: str, value: str) -> bool:
    v2 = value.strip().lower()
    for f in _safe_features(row):
        if f.startswith(f"{category}: "):
            fv = f.split(": ", 1)[1].strip().lower()
            if v2 in fv:
                return True
    return False


def _feature_rank_ok(row: dict, min_rank: int) -> bool:
    from config import energy_rank
    for f in _safe_features(row):
        if f.startswith("Energy: "):
            val = f.split(": ", 1)[1].strip()
            rank = energy_rank(val)
            return rank is not None and rank <= min_rank
    return False


def _list_listings():
    role = api_auth.current_role()
    user = get_current_user() if role == "user" else None
    if role == "user" and user is None:
        return _err.err_unauthorized("用户已被删除")

    # 参数解析
    try:
        limit = max(1, min(int(request.args.get("limit", 100)), 500))
    except (TypeError, ValueError):
        limit = 100
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0
    status = request.args.get("status") or None
    q = request.args.get("q") or None

    # cities: 优先用逗号分隔的多选，回退到单 city
    cities_raw = request.args.get("cities") or None
    if cities_raw:
        cities_list = [c.strip() for c in cities_raw.split(",") if c.strip()]
    else:
        single_city = request.args.get("city") or None
        cities_list = [single_city] if single_city else []

    types_raw = request.args.get("types") or None
    types_list = [t.strip() for t in types_raw.split(",") if t.strip()] if types_raw else []

    contract = request.args.get("contract") or None
    energy = request.args.get("energy") or None

    # SQL：单城市走 SQL 过滤（比 Python 过滤快）；多城市或不选走全量
    sql_city = cities_list[0] if len(cities_list) == 1 else None
    SQL_HARD_CAP = 2000
    with storage_ctx() as st:
        rows = st.get_all_listings(
            status=status, search=q, city=sql_city, limit=SQL_HARD_CAP,
        )

    # user 视角：先应用 listing_filter
    filtered = apply_user_filter(rows, user) if role == "user" else rows

    # Python 端浏览筛选（与 Web 端 /listings 逻辑一致）
    if len(cities_list) > 1:
        cf_lower = {c.lower() for c in cities_list}
        filtered = [r for r in filtered if (r.get("city") or "").lower() in cf_lower]
    if types_list:
        filtered = [r for r in filtered if any(
            _feature_contains(r, "Type", t) for t in types_list)]
    if contract:
        filtered = [r for r in filtered if _feature_contains(r, "Contract", contract)]
    if energy:
        from config import energy_rank
        min_rank = energy_rank(energy)
        if min_rank is not None:
            filtered = [r for r in filtered if _feature_rank_ok(r, min_rank)]

    total = len(filtered)
    page = filtered[offset : offset + limit]
    return _err.ok({
        "items": [serialize_listing(r) for r in page],
        "total": total,
        "limit": limit,
        "offset": offset,
        "filtered": role == "user" and user is not None and not user.listing_filter.is_empty(),
    })


def _get_listing(listing_id: str):
    role = api_auth.current_role()
    user = get_current_user() if role == "user" else None
    if role == "user" and user is None:
        return _err.err_unauthorized("用户已被删除")

    with storage_ctx() as st:
        # listings 表用 id 主键，直接 SQL 单查更省事；现成接口没有，走通用 query
        row = st.conn.execute(
            "SELECT * FROM listings WHERE id = ?",
            (listing_id,),
        ).fetchone()
    if not row:
        return _err.err_not_found("房源不存在")
    r = dict(row)

    # user 视角：filter 不放行的房源对该用户来说"不存在"，避免泄漏
    # 用户口径之外的房源信息（即便房源本身不算敏感，也保持视图一致性）
    if role == "user" and user is not None and not user.listing_filter.is_empty():
        kept = apply_user_filter([r], user)
        if not kept:
            return _err.err_not_found("房源不存在")
    return _err.ok(serialize_listing(r))


def register(bp: Blueprint) -> None:
    bp.add_url_rule(
        "/listings",
        endpoint="listings_list",
        view_func=api_auth.bearer_optional(_list_listings),
        methods=["GET"],
    )
    bp.add_url_rule(
        "/listings/<string:listing_id>",
        endpoint="listings_detail",
        view_func=api_auth.bearer_optional(_get_listing),
        methods=["GET"],
    )
