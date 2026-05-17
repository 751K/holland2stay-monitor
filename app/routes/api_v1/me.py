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
import time as _time
from dataclasses import asdict, replace
from typing import Any

from flask import Blueprint, request

from app import api_auth, api_errors as _err
from config import ENERGY_LABELS, ListingFilter
from users import update_users

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
    - 立即写回 SQLite ``user_configs``（``save_users`` 事务写入）
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

    try:
        def _replace_filter(all_users):
            target_idx = next(
                (i for i, u in enumerate(all_users) if u.id == user.id), None
            )
            if target_idx is None:
                raise LookupError("missing")
            all_users[target_idx] = replace(
                all_users[target_idx],
                listing_filter=new_filter,
            )
            return all_users[target_idx]

        updated_user = update_users(_replace_filter)
    except RuntimeError as e:
        logger.error("用户配置迁移/加载失败: %s", e)
        return _err.err_server_error(e, "用户配置文件损坏")
    except LookupError:
        return _err.err_unauthorized("用户已被删除")
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
        "filter": serialize_filter(updated_user),
        "is_empty": updated_user.listing_filter.is_empty(),
    })


def _filter_options():
    """
    GET /api/v1/filter/options — 列出 FilterEditView 用到的所有候选值。

    返回：
        {
          "cities":        [str, ...],   # 来自 listings.city DISTINCT
          "occupancy":     [str, ...],   # 来自 features "Occupancy:" 前缀
          "types":         [str, ...],
          "neighborhoods": [str, ...],
          "contract":      [str, ...],
          "tenant":        [str, ...],
          "offer":         [str, ...],
          "finishing":     [str, ...],
          "energy":        ["A+++", "A++", "A+", "A", "B", "C", "D", "E", "F"]
        }

    与 ``GET /me/filter`` 配合：客户端拿到 options + 当前 filter，
    在 FilterEditView 里直接渲染勾选状态。bearer_optional：guest 也可读
    （没有过滤条件就纯展示用）。
    """
    with storage_ctx() as st:
        data = {
            "cities":        st.get_distinct_cities(),
            "occupancy":     st.get_feature_values("Occupancy"),
            "types":         st.get_feature_values("Type"),
            "neighborhoods": st.get_feature_values("Neighborhood"),
            "contract":      st.get_feature_values("Contract"),
            "tenant":        st.get_feature_values("Tenant"),
            "offer":         st.get_feature_values("Offer"),
            "finishing":     st.get_feature_values("Finishing"),
            "energy":        list(ENERGY_LABELS),
        }
    return _err.ok(data)


def _delete_account() -> Any:
    """
    DELETE /me — 用户注销自己的账号。

    执行：
    1. 撤销该用户所有 App token（立即生效，不需要等过期）
    2. 从 SQLite user_configs 中删除该用户
    3. 返回 success

    admin 不能通过此端点删除（admin 没有 user_id）。
    """
    role = api_auth.current_role()
    user = get_current_user()
    if user is None:
        return _err.err_unauthorized("用户不存在")

    with storage_ctx() as st:
        revoked = st.revoke_user_tokens(user.id)
        logger.info("账号注销 user=%s name=%r 撤销了 %d 个 token", user.id, user.name, revoked)

    try:
        def _remove_user(all_users):
            new_users = [u for u in all_users if u.id != user.id]
            if len(new_users) == len(all_users):
                raise LookupError("missing")
            all_users[:] = new_users

        update_users(_remove_user)
    except LookupError:
        return _err.err_not_found("用户不存在")
    except RuntimeError as e:
        logger.exception("load_users 失败")
        return _err.err_server_error(e, "用户数据加载失败")
    except Exception as e:
        logger.exception("save_users 失败")
        return _err.err_server_error(e, "账号注销失败")

    logger.info("账号注销完成 user=%s name=%r", user.id, user.name)
    return _err.ok({"deleted": True, "user_id": user.id})


def _export():
    """
    GET /me/export — GDPR 数据导出。

    返回当前 user 的完整个人数据 JSON，包含：
    - 账户信息（name, id, role, created_at）
    - 通知过滤条件
    - 通知历史（最近 500 条）
    - 活跃设备/令牌
    """
    role = api_auth.current_role()
    if role != "user":
        return _err.err_forbidden("仅 user 角色可导出数据")
    user = get_current_user()
    if user is None:
        return _err.err_unauthorized("用户不存在")

    with storage_ctx() as st:
        # 通知历史（该 user_id）
        notif_rows = st.conn.execute(
            "SELECT id, created_at, type, title, body, url, listing_id, read "
            "FROM web_notifications WHERE user_id = ? "
            "ORDER BY id DESC LIMIT 500",
            (user.id,)
        ).fetchall()
        notifications = [dict(r) for r in notif_rows]

        # 活跃设备令牌
        device_rows = st.conn.execute(
            "SELECT dt.id, dt.device_token, dt.env, dt.platform, dt.model, "
            "       dt.bundle_id, dt.created_at, dt.last_seen "
            "FROM device_tokens dt "
            "JOIN app_tokens at ON dt.app_token_id = at.id "
            "WHERE at.user_id = ? AND dt.disabled_at IS NULL",
            (user.id,)
        ).fetchall()
        devices = [dict(r) for r in device_rows]

        # 活跃 App 令牌
        token_rows = st.conn.execute(
            "SELECT id, device_name, created_at, last_used_at, expires_at "
            "FROM app_tokens "
            "WHERE user_id = ? AND revoked = 0",
            (user.id,)
        ).fetchall()
        tokens = [dict(r) for r in token_rows]

    data = {
        "account": {
            "id": user.id,
            "name": user.name,
            "role": role,
            "created_at": user.created_at if hasattr(user, "created_at") else "",
            "enabled": user.enabled,
            "notifications_enabled": user.notifications_enabled,
        },
        "filter": asdict(user.listing_filter),
        "notification_history": notifications,
        "notification_count": len(notifications),
        "active_devices": devices,
        "active_tokens": tokens,
        "exported_at": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
    }
    return _err.ok(data)


def register(bp: Blueprint) -> None:
    bp.add_url_rule(
        "/me/export",
        endpoint="me_export",
        view_func=api_auth.bearer_required(("user",))(_export),
        methods=["GET"],
    )
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
    bp.add_url_rule(
        "/filter/options",
        endpoint="filter_options",
        view_func=api_auth.bearer_optional(_filter_options),
        methods=["GET"],
    )
    bp.add_url_rule(
        "/me",
        endpoint="me_delete",
        view_func=api_auth.bearer_required(("user",))(_delete_account),
        methods=["DELETE"],
    )
