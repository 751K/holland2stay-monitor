"""
路由：App 会话 + 推送设备管理
===============================

挂载的 endpoint
- GET  /settings/app-accounts              → app_accounts（admin，双 tab）
- POST /settings/app-accounts/<token_id>/revoke → app_accounts_revoke
- POST /settings/app-accounts/<token_id>/test-push → app_accounts_test_push
- POST /settings/app-accounts/devices/<device_id>/disable → app_accounts_disable_device

Tab:
- ?tab=sessions  → app_tokens 登录会话（默认）
- ?tab=devices   → device_tokens 推送设备
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

TAB_SESSIONS = "sessions"
TAB_DEVICES = "devices"


def _name_for_user_id(user_id: str, users) -> str:
    if not user_id:
        return ""
    u = next((x for x in users if x.id == user_id), None)
    return u.name if u else user_id


def _user_id_to_name(users) -> dict[str, str]:
    """预构建 user_id → name 映射。"""
    return {u.id: u.name for u in users}


@admin_required
def app_accounts() -> str:
    tab = request.args.get("tab", TAB_SESSIONS)
    if tab not in (TAB_SESSIONS, TAB_DEVICES):
        tab = TAB_SESSIONS

    st = storage()
    users = load_users()
    user_names = _user_id_to_name(users)

    sessions = []
    show_revoked = False
    devices = []

    try:
        if tab == TAB_SESSIONS:
            show_revoked = request.args.get("show_revoked", "").lower() in ("1", "true", "yes")
            sessions = st.list_app_tokens(include_revoked=show_revoked)
            for r in sessions:
                r["user_name"] = user_names.get(r.get("user_id", ""), r.get("user_id", ""))
        else:
            devices = st.list_all_devices()
            for d in devices:
                d["user_name"] = user_names.get(d.get("user_id", ""), d.get("user_id", ""))
                # mask token for display
                tok = d.get("device_token") or ""
                d["token_hint"] = (tok[:8] + "…" + tok[-4:]) if len(tok) > 16 else tok
    finally:
        st.close()

    return render_template(
        "app_accounts.html",
        tab=tab,
        tokens=sessions,
        show_revoked=show_revoked,
        devices=devices,
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
    nxt = request.args.get("next") or request.referrer or url_for("app_accounts")
    return redirect(nxt)


@admin_required
@csrf_required
def app_accounts_test_push(token_id: int) -> Any:
    """向指定 token 的所有活跃设备发送测试推送（APNs + FCM）。"""
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

    # 按平台分流
    ios_devs = [d for d in active if d.get("platform", "ios") != "android"]
    android_devs = [d for d in active if d.get("platform", "ios") == "android"]

    msgs: list[str] = []

    # APNs
    if ios_devs:
        from notifier_channels.apns import ApnsClient, ApnsConfig
        cfg = ApnsConfig.from_env()
        if cfg is None:
            msgs.append("APNs 未启用")
        else:
            payload = {
                "aps": {
                    "alert": {
                        "title": "🧪 Test Push",
                        "body": f"Admin test push（{token_row['device_name'] or 'unknown'}）",
                    },
                    "sound": "default",
                    "thread-id": "test",
                    "badge": 1,
                },
                "kind": "test",
            }
            targets = [{"device_token": d["device_token"], "env": d["env"]} for d in ios_devs]
            import asyncio

            async def _send_apns():
                local = ApnsClient(cfg)
                try:
                    return await local.send_many(targets, payload=payload)
                finally:
                    await local.close()
            try:
                results = asyncio.run(_send_apns())
                sent = sum(1 for r in results if r.ok)
                msgs.append(f"APNs: {sent}/{len(ios_devs)}")
            except Exception as e:
                logger.exception("APNs test push 失败")
                msgs.append(f"APNs: 失败 ({e})")

    # FCM
    if android_devs:
        from notifier_channels.fcm import FcmClient, FcmConfig
        cfg = FcmConfig.from_env()
        if cfg is None:
            msgs.append("FCM 未启用")
        else:
            payload = {
                "message": {
                    "data": {
                        "title": "🧪 Test Push",
                        "body": f"Admin test push（{token_row['device_name'] or 'unknown'}）",
                        "kind": "test",
                        "deep_link": "",
                    },
                    "android": {"priority": "high"},
                },
            }
            targets = [{"device_token": d["device_token"]} for d in android_devs]
            import asyncio

            async def _send_fcm():
                local = FcmClient(cfg)
                try:
                    return await local.send_many(targets, payload=payload)
                finally:
                    await local.close()
            try:
                results = asyncio.run(_send_fcm())
                sent = sum(1 for r in results if r.ok)
                msgs.append(f"FCM: {sent}/{len(android_devs)}")
            except Exception as e:
                logger.exception("FCM test push 失败")
                msgs.append(f"FCM: 失败 ({e})")

    if msgs:
        logger.info("Web test push token_id=%d: %s", token_id, " | ".join(msgs))
        flash("测试推送: " + " | ".join(msgs), "success" if any("/" in m for m in msgs) else "warning")
    else:
        flash("没有可用的推送渠道", "warning")

    return redirect(url_for("app_accounts"))


@admin_required
@csrf_required
def app_accounts_disable_device(device_id: int) -> Any:
    """禁用一台推送设备。"""
    st = storage()
    try:
        ok = st.disable_device(device_id, reason="admin_manual")
    finally:
        st.close()
    if ok:
        flash(f"设备 #{device_id} 已禁用", "success")
    else:
        flash(f"设备 #{device_id} 禁用失败（可能已禁用）", "warning")
    return redirect(url_for("app_accounts", tab=TAB_DEVICES))


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
    app.add_url_rule(
        "/settings/app-accounts/devices/<int:device_id>/disable",
        endpoint="app_accounts_disable_device",
        view_func=app_accounts_disable_device,
        methods=["POST"],
    )
