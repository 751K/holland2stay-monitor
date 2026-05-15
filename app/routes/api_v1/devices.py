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
    import asyncio
    token_id = api_auth.current_token_id()
    if token_id is None:
        return _err.err_unauthorized()

    body = request.get_json(silent=True) or {}
    title = (body.get("title") or "").strip()[:64] or "🧪 测试推送"
    body_text = (body.get("body") or "").strip()[:180] or \
        "如果你在锁屏看到这条，APNs 链路工作正常 ✓"
    apns_only = bool(body.get("apns_only"))
    notification_only = bool(body.get("notification_only"))

    # ── 1. 写 web_notifications（触发 SSE） ────────────────────────
    notification_id: int | None = None
    if not apns_only:
        with storage_ctx() as st:
            notification_id = st.add_web_notification(
                type="new_listing",
                title=title,
                body=body_text,
                listing_id="",
            )
        logger.info("test push 已写 web_notifications id=%d", notification_id)

    # ── 2. 发 APNs ───────────────────────────────────────────────
    sent = 0
    detail: list[dict] = []
    if not notification_only:
        # ⚠️ 不能复用 mcore.push 的单例 ApnsClient：
        # 单例内的 httpx.AsyncClient 第一次 asyncio.run() 时绑到了那次
        # event loop，loop 关掉后再次 asyncio.run() 调它就抛 RuntimeError
        # ("Event loop is closed" / "Task is attached to a different loop")。
        # monitor.py 没问题因为整个进程跑在同一个 loop。
        # 这里建一次性 local client，asyncio.run 内部 close 干净。
        from notifier_channels.apns import ApnsClient, ApnsConfig
        cfg = ApnsConfig.from_env()
        if cfg is None:
            return _err.err_validation("APNs 未启用（后端缺少 .p8 或 APNS_* 配置）")

        with storage_ctx() as st:
            all_devices = st.list_devices_for_token(token_id)
        active = [d for d in all_devices if not d.get("disabled_at")]
        if not active and not apns_only:
            logger.info("test push: SSE 已写，但无设备 APNs 不发")
        elif not active:
            return _err.err_validation("当前会话没有注册过设备")
        else:
            payload = {
                "aps": {
                    "alert": {"title": title, "body": body_text},
                    "sound": "default",
                    "thread-id": "test",
                    "badge": 1,
                },
                "kind": "test",
            }
            targets = [
                {"device_token": d["device_token"], "env": d["env"]}
                for d in active
            ]

            async def _run_once() -> list:
                """Local client 跑完后立即 aclose；避免跨 loop 状态泄漏。"""
                local = ApnsClient(cfg)
                try:
                    return await local.send_many(targets, payload=payload)
                finally:
                    await local.close()

            try:
                results = asyncio.run(_run_once())
            except Exception as e:
                logger.exception("test push 发送异常")
                return _err.err_server_error(e, "推送发送失败")
            sent = sum(1 for r in results if r.ok)
            for d, r in zip(active, results):
                tok = d["device_token"]
                detail.append({
                    "device_token_hint": f"{tok[:12]}…{tok[-4:]}" if len(tok) > 16 else tok,
                    "env": d["env"],
                    "status": r.status,
                    "reason": r.reason,
                    "ok": r.ok,
                })
            logger.info(
                "test push 完成 token_id=%d sent=%d/%d notif_id=%s",
                token_id, sent, len(results), notification_id,
            )

    return _err.ok({
        "sent": sent,
        "total": len(detail),
        "results": detail,
        "notification_id": notification_id,
    })


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
