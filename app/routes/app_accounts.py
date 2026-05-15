"""
路由：App 会话（Bearer Token）管理
====================================

挂载的 endpoint
- GET  /settings/app-accounts                → app_accounts（页面，admin）
- POST /settings/app-accounts/<int:token_id>/revoke → app_accounts_revoke（撤销）

设计要点
--------
- 仅 admin 可见/可改：guest 与 user 在 Web 端不需要管理 Bearer Token
  （他们各自的会话信息也可以放进 /api/v1/me/sessions，留给 Phase 后期）。
- 撤销后立即调 invalidate_token_cache —— 不然 5 分钟内 token 仍可用。
- 包含已撤销的列在默认视图中隐藏，可通过 ?show_revoked=1 切换。
"""

from __future__ import annotations

import logging
from typing import Any

from flask import Flask, flash, redirect, render_template, request, url_for

from app.api_auth import invalidate_token_cache
from app.auth import admin_required
from app.csrf import csrf_required
from app.db import storage
from users import load_users

logger = logging.getLogger(__name__)


def _name_for_user_id(user_id: str, users) -> str:
    """把 user_id 翻译成显示名；找不到时显示 id 本身。"""
    if not user_id:
        return ""
    u = next((x for x in users if x.id == user_id), None)
    return u.name if u else user_id


@admin_required
def app_accounts() -> str:
    show_revoked = request.args.get("show_revoked", "").lower() in ("1", "true", "yes")
    st = storage()
    try:
        rows = st.list_app_tokens(include_revoked=show_revoked)
    finally:
        st.close()
    users = load_users()
    # 为模板预先把 user_id → name 解析好，避免 Jinja 里循环查找
    for r in rows:
        r["user_name"] = _name_for_user_id(r.get("user_id", ""), users)
    return render_template(
        "app_accounts.html",
        tokens=rows,
        show_revoked=show_revoked,
    )


@admin_required
@csrf_required
def app_accounts_revoke(token_id: int) -> Any:
    st = storage()
    try:
        ok = st.revoke_app_token(token_id)
    finally:
        st.close()
    if ok:
        invalidate_token_cache()
        logger.info("admin 撤销了 App token id=%d", token_id)
        flash("会话已撤销", "success")
    else:
        flash("撤销失败（可能已撤销）", "warning")
    # 保持当前过滤态（show_revoked）回到列表
    nxt = request.args.get("next") or request.referrer or url_for("app_accounts")
    return redirect(nxt)


def register(app: Flask) -> None:
    app.add_url_rule(
        "/settings/app-accounts",
        endpoint="app_accounts",
        view_func=app_accounts,
        methods=["GET"],
    )
    app.add_url_rule(
        "/settings/app-accounts/<int:token_id>/revoke",
        endpoint="app_accounts_revoke",
        view_func=app_accounts_revoke,
        methods=["POST"],
    )
