"""
API v1 设备端点
================

- POST   /api/v1/devices/register   注册或刷新一台设备的 APNs token
- GET    /api/v1/devices            列出当前会话名下的设备
- DELETE /api/v1/devices/<id>       主动登出某设备（用户在 App 设置里点）

权限模型
--------
- 全部要求 Bearer（admin/user 都可调）；
  admin 自己也可以登记设备用于调试，但 push.dispatch 只对 user 角色
  关联的设备发送，admin 会话不会收到 user-scoped 通知。
- 同一会话只能管理自己的设备；list/delete 通过 ``app_token_id`` 约束。
"""

from __future__ import annotations

import logging

from flask import Blueprint, request

from app import api_auth, api_errors as _err

from ._helpers import storage_ctx

logger = logging.getLogger(__name__)

_VALID_ENVS = {"production", "sandbox"}


def _register():
    body = request.get_json(silent=True) or {}
    device_token = (body.get("device_token") or "").strip()
    env = (body.get("env") or "production").strip().lower()
    platform = (body.get("platform") or "ios").strip().lower()
    model = (body.get("model") or "").strip()[:64]
    bundle_id = (body.get("bundle_id") or "").strip()[:128]

    if not device_token:
        return _err.err_validation("缺少 device_token")
    # APNs token 现在通常是 64 字符 hex；接受 32-256 范围以兼容未来变化
    if not (32 <= len(device_token) <= 256):
        return _err.err_validation("device_token 长度异常")
    if env not in _VALID_ENVS:
        return _err.err_validation(f"env 必须是 {sorted(_VALID_ENVS)} 之一")

    token_id = api_auth.current_token_id()
    if token_id is None:
        # bearer_required 已经守门；保险起见再检
        return _err.err_unauthorized()

    with storage_ctx() as st:
        try:
            device_id = st.register_device(
                app_token_id=token_id,
                device_token=device_token,
                env=env,
                platform=platform,
                model=model,
                bundle_id=bundle_id,
            )
        except ValueError as e:
            return _err.err_validation(str(e))

    logger.info(
        "device 注册 role=%s user_id=%s token_id=%d device_id=%d env=%s model=%r",
        api_auth.current_role(), api_auth.current_user_id(),
        token_id, device_id, env, model,
    )
    return _err.ok({
        "device_id": device_id,
        "env": env,
        "platform": platform,
    })


def _list():
    token_id = api_auth.current_token_id()
    if token_id is None:
        return _err.err_unauthorized()
    with storage_ctx() as st:
        rows = st.list_devices_for_token(token_id)
    # 不要把完整 device_token 回显给客户端（敏感推送目标）；
    # 只返回前 12 + 末 4 让用户能识别
    safe: list[dict] = []
    for r in rows:
        tok = r["device_token"]
        safe.append({
            "id": r["id"],
            "device_token_hint": f"{tok[:12]}…{tok[-4:]}" if len(tok) > 16 else tok,
            "env": r["env"],
            "platform": r["platform"],
            "model": r.get("model") or "",
            "created_at": r["created_at"],
            "last_seen": r["last_seen"],
            "disabled": bool(r.get("disabled_at")),
            "disabled_reason": r.get("disabled_reason") or "",
        })
    return _err.ok({"items": safe})


def _delete(device_id: int):
    token_id = api_auth.current_token_id()
    if token_id is None:
        return _err.err_unauthorized()
    with storage_ctx() as st:
        # 通过 token 隔离：找不到 = 不是本会话的设备 → 404（不泄漏存在性）
        row = st.get_device(device_id)
        if row is None or row["app_token_id"] != token_id:
            return _err.err_not_found("设备不存在")
        deleted = st.delete_device(device_id)
    return _err.ok({"deleted": bool(deleted)})


def register(bp: Blueprint) -> None:
    bp.add_url_rule(
        "/devices/register",
        endpoint="devices_register",
        view_func=api_auth.bearer_required(("admin", "user"))(_register),
        methods=["POST"],
    )
    bp.add_url_rule(
        "/devices",
        endpoint="devices_list",
        view_func=api_auth.bearer_required(("admin", "user"))(_list),
        methods=["GET"],
    )
    bp.add_url_rule(
        "/devices/<int:device_id>",
        endpoint="devices_delete",
        view_func=api_auth.bearer_required(("admin", "user"))(_delete),
        methods=["DELETE"],
    )
