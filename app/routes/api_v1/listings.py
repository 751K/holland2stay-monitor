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
- source        单平台 SQL 过滤（如 "holland2stay" / "ourdomain"）
- sources       多平台，逗号分隔
- q             名称模糊搜索
- types         房型过滤，逗号分隔（如 "Studio,Apartment"）
- occupancies   入住人数过滤，逗号分隔（如 "Single,Two"）
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

from flask import Blueprint, request

from app import api_auth, api_errors as _err
from app.services.listing_service import (
    get_listing_detail,
    query_listing_rows,
    serialize_listing,
)

from ._helpers import get_current_user


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
    occupancies_raw = request.args.get("occupancies") or None
    occupancies_list = (
        [o.strip() for o in occupancies_raw.split(",") if o.strip()]
        if occupancies_raw else []
    )
    sources_raw = request.args.get("sources") or None
    if sources_raw:
        sources_list = [s.strip() for s in sources_raw.split(",") if s.strip()]
    else:
        single_source = request.args.get("source") or None
        sources_list = [single_source] if single_source else []

    contract = request.args.get("contract") or None
    energy = request.args.get("energy") or None

    SQL_HARD_CAP = 2000
    filtered = query_listing_rows(
        user=user if role == "user" else None,
        status=status,
        search=q,
        cities=cities_list,
        sources=sources_list,
        types=types_list,
        occupancies=occupancies_list,
        contract=contract,
        energy=energy,
        limit=SQL_HARD_CAP,
    )

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

    row = get_listing_detail(listing_id, user if role == "user" else None)
    if row is None:
        return _err.err_not_found("房源不存在")
    return _err.ok(serialize_listing(row))


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
