"""
API v1 通知端点
================

- GET  /api/v1/notifications        分页通知列表（admin/user）
- POST /api/v1/notifications/read   标记已读（全部或指定 ids）
- GET  /api/v1/notifications/stream SSE 推送增量通知
- POST /api/v1/notifications/stream/ticket 用 Bearer 换短期 SSE 票据

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
SSE EventSource 默认不支持自定义 header。能设 header 的客户端直接带
``Authorization: Bearer``；不能设的客户端先调 ``POST .../stream/ticket``
换一枚 60 秒有效、只能用于 SSE 的签名票据，再用 ``?ticket=`` 连接。

**不接受 ``?token=``**：长期 token 放进 URL 会原样落进访问日志，撤销前
一直可复用。票据即使落进日志，60 秒后也失效，且只能读通知。
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


def _ticket():
    """用长期 Bearer 换一枚 60 秒、只能连 SSE 的票据。

    ``bearer_required`` 已经完成鉴权并把 token id 放进 ``g``。
    """
    return _err.ok({
        "ticket": api_auth.issue_sse_ticket(g.api_token_id),
        "expires_in": api_auth.SSE_TICKET_MAX_AGE_SEC,
    })


def _stream():
    """
    SSE 增量通知流。

    鉴权：``Authorization: Bearer xxx``，或 ``?ticket=xxx``
    （先调 ``POST .../stream/ticket`` 换票）。不接受 ``?token=``。
    """
    # 自己处理鉴权（不走装饰器，因为 EventSource 没法带 header）
    tok = request.headers.get("Authorization", "")
    row = None
    if tok.lower().startswith("bearer "):
        plaintext = tok[7:].strip()
        if plaintext:
            row = api_auth._resolve_token(plaintext)
    else:
        ticket = (request.args.get("ticket") or "").strip()
        if ticket:
            row = api_auth.resolve_sse_ticket(ticket)
    if not row:
        return _err.err_unauthorized("token/ticket 无效或已过期")
    if not api_auth._user_token_still_allowed(row):
        return _err.err_unauthorized("账户已禁用")
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
    bp.add_url_rule(
        "/notifications/stream/ticket",
        endpoint="notifications_stream_ticket",
        view_func=api_auth.bearer_required(("admin", "user"))(_ticket),
        methods=["POST"],
    )
