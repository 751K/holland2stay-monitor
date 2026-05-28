"""
Shared APNs device service.

API routes keep authentication and response envelopes; this module owns device
validation, token-safe listing, ownership checks, and APNs test-send behavior.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from app.services.listing_service import storage_ctx

logger = logging.getLogger(__name__)

VALID_ENVS = {"production", "sandbox"}


@dataclass
class DeviceValidationError(Exception):
    """Validation error that routes should expose as a 400 response."""

    message: str

    def __str__(self) -> str:
        return self.message


def _token_hint(token: str) -> str:
    return f"{token[:12]}…{token[-4:]}" if len(token) > 16 else token


def register_device_for_token(
    *,
    token_id: int,
    device_token: str,
    env: str = "production",
    platform: str = "ios",
    model: str = "",
    bundle_id: str = "",
    language: str = "en",
) -> dict:
    """Validate and register/refresh a device for one app auth token."""
    device_token = (device_token or "").strip()
    env = (env or "production").strip().lower()
    platform = (platform or "ios").strip().lower()
    model = (model or "").strip()[:64]
    bundle_id = (bundle_id or "").strip()[:128]
    language = (language or "en").strip().lower()[:8]

    if not device_token:
        raise DeviceValidationError("缺少 device_token")
    if not (32 <= len(device_token) <= 256):
        raise DeviceValidationError("device_token 长度异常")
    if env not in VALID_ENVS:
        raise DeviceValidationError(f"env 必须是 {sorted(VALID_ENVS)} 之一")

    with storage_ctx() as st:
        try:
            device_id = st.register_device(
                app_token_id=token_id,
                device_token=device_token,
                env=env,
                platform=platform,
                model=model,
                bundle_id=bundle_id,
                language=language,
            )
        except ValueError as exc:
            raise DeviceValidationError(str(exc)) from exc

    return {"device_id": device_id, "env": env, "platform": platform}


def list_devices_for_token_safe(*, token_id: int) -> dict:
    """List devices for one app auth token without returning raw APNs tokens."""
    with storage_ctx() as st:
        rows = st.list_devices_for_token(token_id)

    safe: list[dict] = []
    for row in rows:
        token = row["device_token"]
        safe.append({
            "id": row["id"],
            "device_token_hint": _token_hint(token),
            "env": row["env"],
            "platform": row["platform"],
            "model": row.get("model") or "",
            "created_at": row["created_at"],
            "last_seen": row["last_seen"],
            "disabled": bool(row.get("disabled_at")),
            "disabled_reason": row.get("disabled_reason") or "",
        })
    return {"items": safe}


def delete_device_for_token(*, token_id: int, device_id: int) -> bool | None:
    """
    Delete a device only if it belongs to this token.

    Returns None when the device is missing or owned by another token so callers
    can return 404 without leaking device existence.
    """
    with storage_ctx() as st:
        row = st.get_device(device_id)
        if row is None or row["app_token_id"] != token_id:
            return None
        return bool(st.delete_device(device_id))


def create_web_test_notification(*, title: str, body: str) -> int:
    """Insert the Web/SSE part of the push test."""
    with storage_ctx() as st:
        return st.add_web_notification(
            type="new_listing",
            title=title,
            body=body,
            listing_id="",
        )


def send_test_push(
    *,
    token_id: int,
    title: str,
    body: str,
    apns_only: bool = False,
    notification_only: bool = False,
) -> dict:
    """
    Run the app's end-to-end notification test for the current auth token.

    The Web notification branch exercises SSE / notification list behavior; the
    APNs / FCM branch sends directly to devices registered under this app token,
    routing by platform (iOS → APNs, Android → FCM).
    """
    title = (title or "").strip()[:64] or "🧪 测试推送"
    body = (body or "").strip()[:180] or "如果你在锁屏看到这条，推送链路工作正常 ✓"

    notification_id: int | None = None
    if not apns_only:
        notification_id = create_web_test_notification(title=title, body=body)
        logger.info("test push 已写 web_notifications id=%d", notification_id)

    sent = 0
    detail: list[dict] = []
    if not notification_only:
        with storage_ctx() as st:
            all_devices = st.list_devices_for_token(token_id)

        active = [d for d in all_devices if not d.get("disabled_at")]
        if not active and not apns_only:
            logger.info("test push: SSE 已写，但无设备 不发")
            return {
                "sent": 0,
                "total": 0,
                "results": [],
                "notification_id": notification_id,
            }
        if not active:
            raise DeviceValidationError("当前会话没有注册过设备")

        # 按 platform 分流：iOS → APNs，Android → FCM
        ios_devices = [d for d in active if d.get("platform", "ios") != "android"]
        android_devices = [d for d in active if d.get("platform", "ios") == "android"]

        # ── iOS / APNs ────────────────────────────────────────────
        if ios_devices:
            from notifier_channels.apns import ApnsClient, ApnsConfig

            apns_cfg = ApnsConfig.from_env()
            if apns_cfg is not None:
                apns_payload = {
                    "aps": {
                        "alert": {"title": title, "body": body},
                        "sound": "default",
                        "thread-id": "test",
                        "badge": 1,
                    },
                    "kind": "test",
                }
                apns_targets = [
                    {"device_token": d["device_token"], "env": d["env"]}
                    for d in ios_devices
                ]

                async def _run_apns() -> list:
                    local = ApnsClient(apns_cfg)
                    try:
                        return await local.send_many(apns_targets, payload=apns_payload)
                    finally:
                        await local.close()

                results = asyncio.run(_run_apns())
                sent += sum(1 for r in results if r.ok)
                for device, result in zip(ios_devices, results):
                    token = device["device_token"]
                    detail.append({
                        "device_token_hint": _token_hint(token),
                        "platform": device.get("platform", "ios"),
                        "env": device["env"],
                        "status": result.status,
                        "reason": result.reason,
                        "ok": result.ok,
                    })
            else:
                logger.warning("test push: APNs 未启用，跳过 %d 台 iOS 设备", len(ios_devices))

        # ── Android / FCM ─────────────────────────────────────────
        if android_devices:
            from notifier_channels.fcm import FcmClient, FcmConfig

            fcm_cfg = FcmConfig.from_env()
            if fcm_cfg is not None:
                fcm_payload = {
                    "message": {
                        "data": {
                            "title": title,
                            "body": body,
                            "kind": "test",
                        },
                    },
                }
                fcm_targets = [
                    {"device_token": d["device_token"]}
                    for d in android_devices
                ]

                async def _run_fcm() -> list:
                    local = FcmClient(fcm_cfg)
                    try:
                        return await local.send_many(
                            fcm_targets, payload=fcm_payload,
                        )
                    finally:
                        await local.close()

                results = asyncio.run(_run_fcm())
                sent += sum(1 for r in results if r.ok)
                for device, result in zip(android_devices, results):
                    token = device["device_token"]
                    detail.append({
                        "device_token_hint": _token_hint(token),
                        "platform": device.get("platform", "android"),
                        "env": device.get("env", ""),
                        "status": result.status,
                        "reason": result.reason,
                        "ok": result.ok,
                    })
            else:
                logger.warning("test push: FCM 未启用，跳过 %d 台 Android 设备", len(android_devices))

        logger.info(
            "test push 完成 token_id=%d sent=%d/%d (ios=%d android=%d) notif_id=%s",
            token_id, sent, len(active), len(ios_devices), len(android_devices),
            notification_id,
        )

    return {
        "sent": sent,
        "total": len(detail),
        "results": detail,
        "notification_id": notification_id,
    }
