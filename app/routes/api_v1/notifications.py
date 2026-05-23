"""
API v1 通知端点
================

- GET  /api/v1/notifications        分页通知列表（admin/user）
- POST /api/v1/notifications/read   标记已读（全部或指定 ids）
- GET  /api/v1/notifications/stream SSE 推送增量通知

user 视角的过滤
---------------
1. ``user_id`` 列过滤：仅返回 ``user_id = <self.id>`` 或 ``user_id = ''``
   （系统通知，所有人可见）的行。自动预订结果会写入具体 ``user_id``，
   避免 A 的成功/失败结果出现在 B 的 App Alerts。
2. ``listing_filter`` 二次过滤：对带 ``listing_id`` 的行，
   从 listings 表反查该房源，应用本人的 listing_filter，不通过则丢弃。
   减少用户在 App 通知中心看到不相关房源的噪音。
3. 类型白名单：user 不应看到 ``error`` / ``heartbeat`` 这种系统类通知。
   只保留 ``new_listing`` / ``status_change`` / ``booking``。

SSE 鉴权
--------
SSE EventSource 默认不支持自定义 header；iOS 端虽然可以用 URLSession
bytes streaming 带 Authorization header，但为了让浏览器/简单客户端也
能用，支持 ``?token=<plaintext>`` query 参数。token 走和 header 一样的
校验路径。
"""

from __future__ import annotations

import logging

from flask import Blueprint, Response, g, request, stream_with_context

from app import api_auth, api_errors as _err
from app.services.notification_service import (
    list_api_notifications,
    mark_api_notifications_read,
    sse_headers,
    stream_notifications,
)

from ._helpers import get_current_user

logger = logging.getLogger(__name__)


# ── 列表 ───────────────────────────────────────────────────────────


def _list_notifications():
    role = api_auth.current_role() or "guest"
    user = get_current_user() if role == "user" else None
    if role == "user" and user is None:
        return _err.err_unauthorized("用户已被删除")

    try:
        limit = max(1, min(int(request.args.get("limit", 50)), 200))
    except (TypeError, ValueError):
        limit = 50
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0

    return _err.ok(list_api_notifications(
        role=role,
        user=user,
        limit=limit,
        offset=offset,
    ))


# ── 标记已读 ────────────────────────────────────────────────────────


def _mark_read():
    role = api_auth.current_role()
    user = get_current_user() if role == "user" else None
    if role == "user" and user is None:
        return _err.err_unauthorized("用户已被删除")

    body = request.get_json(silent=True) or {}
    ids = body.get("ids")
    if ids is not None:
        if not isinstance(ids, list):
            return _err.err_validation("ids 必须是数组")
        try:
            ids = [int(i) for i in ids]
        except (ValueError, TypeError):
            return _err.err_validation("ids 元素必须是整数")

    mark_api_notifications_read(role=role, user=user, ids=ids)
    return _err.ok({"marked": True})


# ── SSE 推送 ────────────────────────────────────────────────────────


def _stream():
    """
    SSE 增量通知流。

    鉴权：``Authorization: Bearer xxx`` 或 ``?token=xxx`` 查询参数。
    URLSession 可用 header；浏览器 EventSource 只能用 query。
    """
    # 自己处理鉴权（不走装饰器，因为 EventSource 没法带 header）
    tok = request.headers.get("Authorization", "")
    if tok.lower().startswith("bearer "):
        tok = tok[7:].strip()
    else:
        tok = (request.args.get("token") or "").strip()
    if not tok:
        return _err.err_unauthorized()
    row = api_auth._resolve_token(tok)
    if not row:
        return _err.err_unauthorized("token 无效或已过期")
    g.api_role = row["role"]
    g.api_user_id = row.get("user_id")
    g.api_token_id = row["id"]
    api_auth._schedule_touch(row["id"])

    role = row["role"]
    user_id = row.get("user_id")
    user = None
    if role == "user":
        user = get_current_user()
        if user is None:
            return _err.err_unauthorized("用户已被删除")

    try:
        last_id = int(request.args.get("last_id", 0))
    except (TypeError, ValueError):
        last_id = 0

    return Response(
        stream_with_context(stream_notifications(
            last_id=last_id,
            role=role,
            user=user,
            user_id=user_id,
        )),
        mimetype="text/event-stream",
        headers=sse_headers(),
    )


def register(bp: Blueprint) -> None:
    bp.add_url_rule(
        "/notifications",
        endpoint="notifications_list",
        view_func=api_auth.bearer_optional(_list_notifications),
        methods=["GET"],
    )
    bp.add_url_rule(
        "/notifications/read",
        endpoint="notifications_read",
        view_func=api_auth.bearer_required(("admin", "user"))(_mark_read),
        methods=["POST"],
    )
    bp.add_url_rule(
        "/notifications/stream",
        endpoint="notifications_stream",
        view_func=_stream,   # 自己处理鉴权（SSE 不能用装饰器）
        methods=["GET"],
    )
