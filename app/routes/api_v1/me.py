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

import logging
from dataclasses import replace
from typing import Any

from flask import Blueprint, request

from app import api_auth, api_errors as _err
from config import ENERGY_LABELS, ListingFilter
from users import load_users, save_users

from ._helpers import (
    apply_user_filter,
    get_current_user,
    serialize_filter,
    storage_ctx,
)

logger = logging.getLogger(__name__)


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


# 可改的字段白名单 + 类型校验函数。
# 不在白名单的字段会被忽略，避免客户端注入未知 attr。
_LIST_FIELDS = {
    "allowed_occupancy",
    "allowed_types",
    "allowed_neighborhoods",
    "allowed_cities",
    "allowed_contract",
    "allowed_tenant",
    "allowed_offer",
    "allowed_finishing",
}
_FLOAT_FIELDS = {"max_rent", "min_area"}
_INT_FIELDS = {"min_floor"}


def _coerce_filter_payload(raw: Any) -> dict[str, Any]:
    """
    把客户端 JSON payload 收成可丢给 ``ListingFilter(...)`` 的 dict。

    规则：
    - None / 缺省的字段：跳过（保持 ListingFilter 默认值）
    - 数值字段：尝试 float/int；负数 / NaN / 非数 → 丢弃
    - 列表字段：必须是 list[str]；非字符串元素被过滤
    - allowed_energy：必须在白名单内（大小写不敏感），否则改为 ""
    - 其它字段：忽略
    """
    if not isinstance(raw, dict):
        raise ValueError("filter 必须是 JSON 对象")

    out: dict[str, Any] = {}
    for k in _FLOAT_FIELDS:
        if k in raw and raw[k] is not None:
            try:
                v = float(raw[k])
                if v > 0 and v < 1e9 and v == v:  # v==v filters NaN
                    out[k] = v
            except (TypeError, ValueError):
                pass
    for k in _INT_FIELDS:
        if k in raw and raw[k] is not None:
            try:
                v = int(raw[k])
                if 0 <= v <= 200:
                    out[k] = v
            except (TypeError, ValueError):
                pass
    for k in _LIST_FIELDS:
        v = raw.get(k)
        if isinstance(v, list):
            cleaned = [str(x).strip() for x in v if isinstance(x, str) and x.strip()]
            out[k] = cleaned[:50]   # 50 上限防滥用
    if "allowed_energy" in raw:
        v = raw.get("allowed_energy")
        if isinstance(v, str):
            upper = v.strip().upper()
            out["allowed_energy"] = upper if upper in ENERGY_LABELS else ""
    return out


def _filter_update():
    """
    PUT /api/v1/me/filter — user 自助修改自己的过滤条件。

    幂等：完整覆盖式更新（缺省字段保留 ListingFilter 默认值，**不**保留旧值）。

    业务效果：
    - 立即写回 ``data/users.json``（``save_users`` 原子替换）
    - 下一轮 ``monitor.run_once`` 检测到文件 mtime 变化会自动 reload 用户配置
      （现有 _load_users_if_changed 逻辑，不需要这里显式触发）
    - APNs 设备绑定不动；推送策略下轮即按新 filter 决定

    Body: ListingFilter 的字段子集，例如
        {"max_rent": 900, "min_area": 25, "allowed_cities": ["Eindhoven"]}
    """
    role = api_auth.current_role()
    if role != "user":
        return _err.err_forbidden("仅 user 角色可修改自己的过滤条件")
    user = get_current_user()
    if user is None:
        return _err.err_unauthorized("用户已被删除")

    body = request.get_json(silent=True) or {}
    try:
        cleaned = _coerce_filter_payload(body)
    except ValueError as e:
        return _err.err_validation(str(e))

    new_filter = ListingFilter(**cleaned)

    # users.json 是 source of truth；重新 load 拿最新（防其他端并发修改丢失）
    try:
        all_users = load_users()
    except RuntimeError as e:
        logger.error("users.json 解析失败: %s", e)
        return _err.err_server_error(e, "用户配置文件损坏")
    target_idx = next(
        (i for i, u in enumerate(all_users) if u.id == user.id), None
    )
    if target_idx is None:
        return _err.err_unauthorized("用户已被删除")

    all_users[target_idx] = replace(all_users[target_idx], listing_filter=new_filter)
    try:
        save_users(all_users)
    except Exception as e:
        logger.exception("save_users 失败")
        return _err.err_server_error(e, "保存失败")

    logger.info(
        "user=%s 自助修改 filter: %s",
        user.id,
        {k: v for k, v in cleaned.items() if v not in (None, [], "")},
    )

    # 重新读 / serialize 后返回，保证客户端与服务端状态一致
    return _err.ok({
        "role": role,
        "filter": serialize_filter(all_users[target_idx]),
        "is_empty": all_users[target_idx].listing_filter.is_empty(),
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
    bp.add_url_rule(
        "/me/filter",
        endpoint="me_filter_update",
        view_func=api_auth.bearer_required(("user",))(_filter_update),
        methods=["PUT"],
    )
