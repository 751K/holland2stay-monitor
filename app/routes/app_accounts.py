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


@admin_required
@csrf_required
def app_accounts_test_push(token_id: int) -> Any:
    """向指定 token 的所有活跃设备发送一条测试 APNs 推送。"""
    st = storage()
    try:
        token_row = st.conn.execute(
            "SELECT id, role, user_id, device_name FROM app_tokens WHERE id = ?",
            (token_id,),
        ).fetchone()
        if token_row is None:
            flash("Token 不存在", "warning")
            return redirect(url_for("app_accounts"))

        devices = st.list_devices_for_token(token_id)
    finally:
        st.close()

    active = [d for d in devices if not d.get("disabled_at")]
    if not active:
        flash(f"该会话没有活跃设备（共 {len(devices)} 台，{len(active)} 台活跃）", "warning")
        return redirect(url_for("app_accounts"))

    # 构造测试 payload
    from notifier_channels.apns import ApnsClient, ApnsConfig
    cfg = ApnsConfig.from_env()
    if cfg is None:
        flash("APNs 未启用（后端缺少 APNS_ENABLED / .p8 / APNS_* 配置）", "danger")
        return redirect(url_for("app_accounts"))

    payload = {
        "aps": {
            "alert": {
                "title": "🧪 Web 面板测试推送",
                "body": f"管理员从 Web 面板发送的测试推送（{token_row['device_name'] or '未知设备'}）",
            },
            "sound": "default",
            "thread-id": "test",
            "badge": 1,
        },
        "kind": "test",
    }
    targets = [{"device_token": d["device_token"], "env": d["env"]} for d in active]

    import asyncio

    async def _send():
        local = ApnsClient(cfg)
        try:
            return await local.send_many(targets, payload=payload)
        finally:
            await local.close()

    try:
        results = asyncio.run(_send())
    except Exception as e:
        logger.exception("Web test push 发送异常 token_id=%d", token_id)
        flash(f"推送发送失败：{e}", "danger")
        return redirect(url_for("app_accounts"))

    sent = sum(1 for r in results if r.ok)
    failed = len(results) - sent
    if sent:
        flash(f"测试推送已发送：{sent}/{len(results)} 台设备成功" + (f"，{failed} 台失败" if failed else ""), "success")
    else:
        flash(f"测试推送失败：{len(results)} 台设备均未成功", "danger")

    device_name = token_row["device_name"] or f"token#{token_id}"
    logger.info("Web test push: admin 向 %s 发送，%d/%d 成功", device_name, sent, len(results))
    return redirect(url_for("app_accounts"))


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
    app.add_url_rule(
        "/settings/app-accounts/<int:token_id>/test-push",
        endpoint="app_accounts_test_push",
        view_func=app_accounts_test_push,
        methods=["POST"],
    )
