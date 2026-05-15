"""
API v1 房源端点
================

- GET /api/v1/listings            房源列表（admin: 全量 / user: 应用 listing_filter）
- GET /api/v1/listings/<id>       单条详情（user 拿不到 filter 之外的房源 → 404）

查询参数（与 Web 端 /listings 一致的子集）
--------
- status        房源状态精确匹配（"Available to book" 等）
- city          单城市 SQL 过滤
- q             名称模糊搜索
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

from ._helpers import (
    apply_user_filter,
    get_current_user,
    serialize_listing,
    storage_ctx,
)


def _list_listings():
    role = api_auth.current_role()
    user = get_current_user() if role == "user" else None
    if role == "user" and user is None:
        # user_id 失效（用户被删了），上层 bearer_required 不会拦到这种
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
    city = request.args.get("city") or None
    q = request.args.get("q") or None

    # 读库：取一个相对宽松的上限再走 Python 过滤 + 切片。这样：
    # 1) total 反映过滤后的真实条数（admin/user 都一致）
    # 2) user 翻页不会跳条（listing_filter 是 Python 侧的）
    # 3) 现实数据 < 2000 条，整列拉取也不昂贵
    SQL_HARD_CAP = 2000
    with storage_ctx() as st:
        rows = st.get_all_listings(
            status=status, search=q, city=city, limit=SQL_HARD_CAP,
        )

    filtered = apply_user_filter(rows, user) if role == "user" else rows
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
        view_func=api_auth.bearer_required(("admin", "user"))(_list_listings),
        methods=["GET"],
    )
    bp.add_url_rule(
        "/listings/<string:listing_id>",
        endpoint="listings_detail",
        view_func=api_auth.bearer_required(("admin", "user"))(_get_listing),
        methods=["GET"],
    )
