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
from app.services.device_service import (
    DeviceValidationError,
    delete_device_for_token,
    list_devices_for_token_safe,
    register_device_for_token,
    send_test_push,
)

logger = logging.getLogger(__name__)


def _register():
    body = request.get_json(silent=True) or {}
    device_token = (body.get("device_token") or "").strip()
    env = (body.get("env") or "production").strip().lower()
    platform = (body.get("platform") or "ios").strip().lower()
    model = (body.get("model") or "").strip()[:64]
    bundle_id = (body.get("bundle_id") or "").strip()[:128]

    token_id = api_auth.current_token_id()
    if token_id is None:
        # bearer_required 已经守门；保险起见再检
        return _err.err_unauthorized()

    try:
        result = register_device_for_token(
            token_id=token_id,
            device_token=device_token,
            env=env,
            platform=platform,
            model=model,
            bundle_id=bundle_id,
        )
    except DeviceValidationError as exc:
        return _err.err_validation(str(exc))

    logger.info(
        "device 注册 role=%s user_id=%s token_id=%d device_id=%d env=%s model=%r",
        api_auth.current_role(), api_auth.current_user_id(),
        token_id, result["device_id"], result["env"], model,
    )
    return _err.ok(result)


def _list():
    token_id = api_auth.current_token_id()
    if token_id is None:
        return _err.err_unauthorized()
    return _err.ok(list_devices_for_token_safe(token_id=token_id))


def _delete(device_id: int):
    token_id = api_auth.current_token_id()
    if token_id is None:
        return _err.err_unauthorized()
    # 通过 token 隔离：找不到 = 不是本会话的设备 → 404（不泄漏存在性）
    deleted = delete_device_for_token(token_id=token_id, device_id=device_id)
    if deleted is None:
        return _err.err_not_found("设备不存在")
    return _err.ok({"deleted": bool(deleted)})


def _test_push():
    """
    POST /api/v1/devices/test — 一键全链路测试。

    1. 向 ``web_notifications`` 表插一条 type=new_listing 行
       → 触发 SSE，iOS 列表顶部即时出现 + tab badge +1
    2. 给当前会话的所有活跃设备发 APNs 推送
       → 锁屏 / 横幅 / app 图标红点（如 iOS 允许 badge）

    body（全部可选）:
      title           : "🧪 测试推送"
      body            : "如果你看到这条..."
      apns_only       : true → 跳过 SSE 写库（只验证 APNs 链路）
      notification_only: true → 跳过 APNs 发送（只验证 SSE 链路）

    返回：
      {
        sent: int, total: int,
        results: [{device_token_hint, status, reason, ok}],
        notification_id: int | null    # 写库行 id；apns_only=true 时为 null
      }

    与 mcore.push.dispatch 的区别
    -----------------------------
    - dispatch 按 user_id 查设备；admin 没 user_id 用不了
    - dispatch 有节流（同 listing/kind 5min 1 条），测试不该受限
    这里绕开 dispatch，直接调 ApnsClient.send_many。
    """
    token_id = api_auth.current_token_id()
    if token_id is None:
        return _err.err_unauthorized()

    body = request.get_json(silent=True) or {}
    title = (body.get("title") or "").strip()[:64] or "🧪 测试推送"
    body_text = (body.get("body") or "").strip()[:180] or \
        "如果你在锁屏看到这条，APNs 链路工作正常 ✓"
    apns_only = bool(body.get("apns_only"))
    notification_only = bool(body.get("notification_only"))

    try:
        return _err.ok(send_test_push(
            token_id=token_id,
            title=title,
            body=body_text,
            apns_only=apns_only,
            notification_only=notification_only,
        ))
    except DeviceValidationError as exc:
        return _err.err_validation(str(exc))
    except Exception as exc:
        logger.exception("test push 发送异常")
        return _err.err_server_error(exc, "推送发送失败")


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
    bp.add_url_rule(
        "/devices/test",
        endpoint="devices_test_push",
        view_func=api_auth.bearer_required(("admin", "user"))(_test_push),
        methods=["POST"],
    )
