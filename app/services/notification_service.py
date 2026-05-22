"""
Shared notification service.

Web routes and API v1 routes expose different response envelopes, but they
should use the same notification filtering, read-state, and SSE streaming
semantics.
"""
from __future__ import annotations

import json
import threading
import time as _time
from typing import Any

from app.services.listing_service import apply_user_filter, storage_ctx

USER_ALLOWED_TYPES = {"new_listing", "status_change", "booking"}
SSE_POLL_SECONDS = 5
SSE_MAX_AGE_SECONDS = 300


def filter_for_user_view(rows: list[dict], user: Any) -> list[dict]:
    """
    Filter notification rows for a user-scoped API view.

    SQL has already narrowed rows to ``user_id = self.id OR user_id = ''``.
    This second pass removes system-only event types and applies the user's
    listing_filter to rows that reference a listing.
    """
    def _visible_type(row: dict) -> bool:
        if row.get("type") not in USER_ALLOWED_TYPES:
            return False
        # Booking 结果是用户私有事件。旧版本曾写成 user_id='' 的全局通知；
        # 用户视角必须隐藏这些旧行，避免 A 的成功/失败出现在 B 的 Alerts。
        if row.get("type") == "booking" and row.get("user_id") != getattr(user, "id", ""):
            return False
        return True

    typed = [r for r in rows if _visible_type(r)]
    if user is None or user.listing_filter.is_empty():
        return typed

    listing_ids = {r["listing_id"] for r in typed if r.get("listing_id")}
    if not listing_ids:
        return typed

    with storage_ctx() as st:
        placeholders = ",".join("?" * len(listing_ids))
        raw = st.conn.execute(
            f"SELECT * FROM listings WHERE id IN ({placeholders})",
            list(listing_ids),
        ).fetchall()

    keep_ids = {r["id"] for r in apply_user_filter([dict(r) for r in raw], user)}
    out: list[dict] = []
    for row in typed:
        listing_id = row.get("listing_id") or ""
        if not listing_id or listing_id in keep_ids:
            out.append(row)
    return out


def list_api_notifications(
    *,
    role: str,
    user: Any,
    limit: int,
    offset: int,
) -> dict:
    """Return the API v1 notification payload shape."""
    with storage_ctx() as st:
        if role == "user":
            raw = st.get_notifications(
                limit=limit * 3 + 200,
                offset=0,
                user_id=user.id,
            )
        else:
            raw = st.get_notifications(limit=limit + offset, offset=0)
        unread = st.count_unread_notifications()

    filtered = filter_for_user_view(raw, user) if role == "user" else raw
    if role == "user":
        unread = sum(1 for row in filtered if not int(row.get("read") or 0))
    total = len(filtered)
    page = filtered[offset : offset + limit]
    return {
        "items": page,
        "total": total,
        "unread": unread,
        "limit": limit,
        "offset": offset,
    }


def list_web_notifications(*, limit: int, offset: int) -> dict:
    """Return the existing Web notification payload fields."""
    with storage_ctx() as st:
        rows = st.get_notifications(limit=limit, offset=offset)
        unread = st.count_unread_notifications()
    return {"notifications": rows, "unread": unread}


def mark_api_notifications_read(
    *,
    role: str,
    user: Any,
    ids: list[int] | None,
) -> None:
    """
    Mark notifications read while preserving user/admin isolation.

    ``ids=None`` means "all visible". A user can only mark notifications whose
    ``user_id`` is their id or the shared system notification id.
    """
    with storage_ctx() as st:
        if role == "user" and ids is None:
            with st.conn:
                st.conn.execute(
                    "UPDATE web_notifications SET read = 1 "
                    "WHERE read = 0 AND (user_id = ? OR user_id = '')",
                    (user.id,),
                )
        elif role == "user" and ids:
            placeholders = ",".join("?" * len(ids))
            with st.conn:
                st.conn.execute(
                    f"UPDATE web_notifications SET read = 1 "
                    f"WHERE id IN ({placeholders}) "
                    f"AND (user_id = ? OR user_id = '')",
                    [*ids, user.id],
                )
        else:
            st.mark_notifications_read(ids=ids)


def mark_web_notifications_read(*, ids: list[int] | None) -> None:
    """Admin Web behavior: mark global notifications read."""
    with storage_ctx() as st:
        st.mark_notifications_read(ids=ids)


def sse_headers() -> dict[str, str]:
    """Common SSE headers for Web and API v1 notification streams."""
    return {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }


def stream_notifications(
    *,
    last_id: int,
    role: str = "admin",
    user: Any = None,
    user_id: str | None = None,
):
    """
    Yield SSE chunks for incremental notifications.

    The generator owns its storage connection because Flask closes request
    scoped resources before streamed responses finish.
    """
    stop = threading.Event()
    expires = _time.monotonic() + SSE_MAX_AGE_SECONDS

    def _generate():
        nonlocal last_id
        yield "retry: 2000\n\n"

        from app.db import storage as _open_storage

        st = _open_storage()
        try:
            while not stop.is_set() and _time.monotonic() < expires:
                rows = st.get_notifications_since(
                    last_id,
                    user_id=user_id if role == "user" else None,
                )
                if role == "user" and rows:
                    rows = filter_for_user_view(rows, user)

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

                stop.wait(SSE_POLL_SECONDS)
        except GeneratorExit:
            pass
        finally:
            st.close()
            stop.set()

    return _generate()
