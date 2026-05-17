"""
API v1 用户反馈端点
======================
POST /api/v1/feedback — 提交 bug 报告或功能建议。

认证用户（user/admin）可提交；guest 不可（防滥用）。
反馈写入 feedback 表，管理员可在后台查看。
"""

from __future__ import annotations

import logging
from typing import Any

from flask import Blueprint, g, request

from app import api_auth, api_errors as _err
from app.db import storage

logger = logging.getLogger(__name__)

ALLOWED_KINDS = {"bug", "suggestion", "other"}


def _submit():
    """POST /api/v1/feedback"""
    body = request.get_json(silent=True) or {}
    kind = str(body.get("kind", "other")).strip().lower()
    message = str(body.get("message", "")).strip()

    if kind not in ALLOWED_KINDS:
        return _err.err_validation(f"kind 必须是 {', '.join(sorted(ALLOWED_KINDS))} 之一")
    if not message or len(message) < 5:
        return _err.err_validation("反馈内容至少 5 个字符")
    if len(message) > 2000:
        return _err.err_validation("反馈内容最长 2000 字符")

    role = api_auth.current_role()
    user_id = getattr(g, "api_user_id", None)

    st = storage()
    try:
        st.conn.execute(
            """CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                kind TEXT NOT NULL,
                message TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT '',
                user_id TEXT NOT NULL DEFAULT '',
                user_name TEXT NOT NULL DEFAULT '',
                app_version TEXT NOT NULL DEFAULT '',
                platform TEXT NOT NULL DEFAULT 'ios'
            )"""
        )
        st.conn.execute(
            "INSERT INTO feedback (kind, message, role, user_id, user_name, app_version) "
            "VALUES (?,?,?,?,?,?)",
            (kind, message, role, user_id or "",
             str(body.get("user_name", "")).strip(),
             str(body.get("app_version", "")).strip()),
        )
        st.conn.commit()
    except Exception as e:
        logger.exception("反馈写入失败")
        return _err.err_server_error(e, "反馈提交失败，请稍后重试")
    finally:
        st.close()

    logger.info("反馈已提交 kind=%s role=%s user=%s len=%d", kind, role, user_id, len(message))
    return _err.ok({"submitted": True})


def register(bp: Blueprint) -> None:
    bp.add_url_rule(
        "/feedback",
        endpoint="feedback_submit",
        view_func=api_auth.bearer_required(("admin", "user"))(_submit),
        methods=["POST"],
    )
