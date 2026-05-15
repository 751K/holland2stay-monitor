"""
API v1 通知端点
================

- GET  /api/v1/notifications        分页通知列表（admin/user）
- POST /api/v1/notifications/read   标记已读（全部或指定 ids）
- GET  /api/v1/notifications/stream SSE 推送增量通知

user 视角的过滤
---------------
1. ``user_id`` 列过滤：仅返回 ``user_id = <self.id>`` 或 ``user_id = ''``
   （系统通知，所有人可见）的行。当前 Phase 2 写入路径还未 populate
   user_id，所以效果等同 "返回全部"；Phase 3 APNs 接入后会真正分流。
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

import json
import logging
import threading
import time as _time

from flask import Blueprint, Response, g, request, stream_with_context

from app import api_auth, api_errors as _err

from ._helpers import (
    apply_user_filter,
    get_current_user,
    storage_ctx,
)

logger = logging.getLogger(__name__)

# 用户视角只关心房源相关事件
_USER_ALLOWED_TYPES = {"new_listing", "status_change", "booking"}


def _filter_for_user_view(rows: list[dict], user) -> list[dict]:
    """
    通知行的 user 视角过滤：
    - 仅保留 _USER_ALLOWED_TYPES 内的类型
    - 对每条带 listing_id 的行反查 Listing，应用本人 listing_filter

    rows 已经由 SQL 层做了 user_id 维度的初筛（user_id = self.id OR '')。
    """
    if user is None or user.listing_filter.is_empty():
        return [r for r in rows if r.get("type") in _USER_ALLOWED_TYPES]
    typed = [r for r in rows if r.get("type") in _USER_ALLOWED_TYPES]
    listing_ids = {r["listing_id"] for r in typed if r.get("listing_id")}
    if not listing_ids:
        return typed
    with storage_ctx() as st:
        placeholders = ",".join("?" * len(listing_ids))
        raw = st.conn.execute(
            f"SELECT * FROM listings WHERE id IN ({placeholders})",
            list(listing_ids),
        ).fetchall()
    keep_ids = {r["id"] for r in apply_user_filter(
        [dict(r) for r in raw], user,
    )}
    out: list[dict] = []
    for r in typed:
        lid = r.get("listing_id") or ""
        if not lid or lid in keep_ids:
            out.append(r)
    return out


# ── 列表 ───────────────────────────────────────────────────────────


def _list_notifications():
    role = api_auth.current_role()
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

    with storage_ctx() as st:
        if role == "user":
            # user_id 维度的 SQL 收窄：拿 user 自己的 + 系统通知
            # 多取一些（user 视角的二次 Python 过滤可能丢条目）
            raw = st.get_notifications(
                limit=limit * 3 + 200,
                offset=0,
                user_id=user.id,  # type: ignore[union-attr]
            )
        else:
            raw = st.get_notifications(limit=limit + offset, offset=0)
        unread = st.count_unread_notifications()

    if role == "user":
        filtered = _filter_for_user_view(raw, user)
    else:
        filtered = raw
    total = len(filtered)
    page = filtered[offset : offset + limit]
    return _err.ok({
        "items": page,
        "total": total,
        "unread": unread,
        "limit": limit,
        "offset": offset,
    })


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

    with storage_ctx() as st:
        if role == "user" and ids is None:
            # 全部已读 → 只针对自己可见的（user_id=self.id 或 '')
            # 用 explicit SQL 限定 user_id，避免 user 误点 "全部已读"
            # 影响别人/admin 的未读计数。
            with st.conn:
                st.conn.execute(
                    "UPDATE web_notifications SET read = 1 "
                    "WHERE read = 0 AND (user_id = ? OR user_id = '')",
                    (user.id,),  # type: ignore[union-attr]
                )
        elif role == "user" and ids:
            # 指定 ids：交集自己可见的，防止越权标记别人的
            placeholders = ",".join("?" * len(ids))
            with st.conn:
                st.conn.execute(
                    f"UPDATE web_notifications SET read = 1 "
                    f"WHERE id IN ({placeholders}) "
                    f"AND (user_id = ? OR user_id = '')",
                    [*ids, user.id],  # type: ignore[union-attr]
                )
        else:
            # admin：保留原全局行为
            st.mark_notifications_read(ids=ids)
    return _err.ok({"marked": True})


# ── SSE 推送 ────────────────────────────────────────────────────────


_SSE_POLL = 5    # 轮询秒数
_SSE_MAXAGE = 300  # 单连接最大生命


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

    stop = threading.Event()
    expires = _time.monotonic() + _SSE_MAXAGE

    def _generate():
        nonlocal last_id
        yield "retry: 2000\n\n"
        # SSE 在 stream_with_context 内不能引用外层 storage()——会立刻 close。
        # 独立开一份连接。
        from app.db import storage as _open_storage
        st = _open_storage()
        try:
            while not stop.is_set() and _time.monotonic() < expires:
                rows = st.get_notifications_since(
                    last_id,
                    user_id=user_id if role == "user" else None,
                )
                if role == "user" and rows:
                    rows = _filter_for_user_view(rows, user)
                if rows:
                    last_id = rows[-1]["id"]
                    payload = json.dumps(rows, ensure_ascii=False)
                    chunk = f"data: {payload}\n\n"
                else:
                    chunk = ": keepalive\n\n"
                try:
                    yield chunk
                except (GeneratorExit, BrokenPipeError, ConnectionResetError, OSError):
                    return
                stop.wait(_SSE_POLL)
        except GeneratorExit:
            pass
        finally:
            st.close()
            stop.set()

    return Response(
        stream_with_context(_generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def register(bp: Blueprint) -> None:
    bp.add_url_rule(
        "/notifications",
        endpoint="notifications_list",
        view_func=api_auth.bearer_required(("admin", "user"))(_list_notifications),
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
