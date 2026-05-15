"""
APNs HTTP/2 客户端
====================

职责
----
对接 Apple Push Notification Service：
- 用 .p8 ES256 私钥签 Provider JWT（每 30 分钟轮换 + 缓存）
- 用 httpx HTTP/2 客户端把 push payload POST 到 ``/3/device/<token>``
- 解析 APNs 的 status/reason，归一化成 ApnsResult

设计原则
--------
- **纯 IO 层**：不读 Storage、不知 user/listing 语义，仅收发字节。
  上层 mcore/push.py 负责调度与节流。
- **无副作用 import**：构造 ApnsClient 时才读 .p8 / JWT 库；
  monitor 进程在 APNS_ENABLED=false 时根本不会构造客户端。
- **失败 fail-closed**：任一异常路径都返回 ``ApnsResult(status=0, reason=...)``，
  不抛到调用方；上层只看 result.status 判断是否需要 disable_device。

测试
----
ApnsClient 接受 ``transport=httpx.MockTransport(...)`` 注入，便于单测；
JWT 部分用真实 cryptography ES256 签名（依赖该库已存在）。
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ── 配置 ────────────────────────────────────────────────────────────


@dataclass
class ApnsConfig:
    """APNs 客户端的运行时配置；通常从 .env 装填。"""

    key_path: str            # .p8 文件路径
    key_id: str              # Apple Developer Key ID（10 字符）
    team_id: str             # Team ID（10 字符）
    topic: str               # Bundle ID，e.g. "com.kong.h2smonitor"
    env_default: str = "production"   # device.env 缺失时的 fallback
    concurrency: int = 16    # send_many 并发上限
    request_timeout: float = 10.0

    @classmethod
    def from_env(cls) -> Optional["ApnsConfig"]:
        """
        从环境变量读配置；APNS_ENABLED!=true 或缺关键字段时返回 None。
        返回 None 即表示"运行时不启用 APNs"，上层 push.dispatch 应当跳过。
        """
        if os.environ.get("APNS_ENABLED", "").lower() != "true":
            return None
        kp = os.environ.get("APNS_KEY_PATH", "").strip()
        kid = os.environ.get("APNS_KEY_ID", "").strip()
        tid = os.environ.get("APNS_TEAM_ID", "").strip()
        topic = os.environ.get("APNS_TOPIC", "").strip()
        missing = [n for n, v in (
            ("APNS_KEY_PATH", kp), ("APNS_KEY_ID", kid),
            ("APNS_TEAM_ID", tid), ("APNS_TOPIC", topic),
        ) if not v]
        if missing:
            logger.warning(
                "APNS_ENABLED=true 但缺少配置 %s，本进程禁用 APNs", missing,
            )
            return None
        if not Path(kp).exists():
            logger.warning("APNs .p8 文件不存在: %s，本进程禁用 APNs", kp)
            return None
        return cls(
            key_path=kp, key_id=kid, team_id=tid, topic=topic,
            env_default=os.environ.get("APNS_ENV_DEFAULT", "production"),
            concurrency=int(os.environ.get("APNS_CONCURRENCY", "16") or 16),
        )


# ── 结果 ────────────────────────────────────────────────────────────


@dataclass
class ApnsResult:
    """单条推送的归一化结果。

    status   : HTTP 状态码；客户端异常时为 0
    reason   : APNs JSON body 的 reason 字段，e.g. "BadDeviceToken"；
               无网络/异常时为本地错误描述
    device   : 原始 device_token，便于上层 disable 时定位
    apns_id  : APNs 服务端给的事件 ID（在 ``apns-id`` 响应头里）；
               用于日志关联，无网络时为空
    """

    status: int
    reason: str
    device: str
    apns_id: str = ""

    @property
    def ok(self) -> bool:
        return self.status == 200

    @property
    def device_dead(self) -> bool:
        """需要把对应 device_token 软停的状态。"""
        return self.status in (400, 410) and self.reason in (
            "BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic",
        )


# ── JWT 签名 ────────────────────────────────────────────────────────


class _JwtSigner:
    """ES256 Provider JWT 生成与缓存（30 分钟轮换）。"""

    REFRESH_INTERVAL = 30 * 60   # APNs 要求 ≥ 20 min、< 60 min

    def __init__(self, key_path: str, key_id: str, team_id: str) -> None:
        from cryptography.hazmat.primitives.serialization import (
            load_pem_private_key,
        )
        with open(key_path, "rb") as fh:
            self._key = load_pem_private_key(fh.read(), password=None)
        self.key_id = key_id
        self.team_id = team_id
        self._token: str = ""
        self._exp: float = 0.0

    def token(self, now: Optional[float] = None) -> str:
        """返回（必要时刷新）当前 JWT。"""
        import jwt as _jwt
        now = now if now is not None else time.time()
        if now < self._exp and self._token:
            return self._token
        self._token = _jwt.encode(
            {"iss": self.team_id, "iat": int(now)},
            self._key,
            algorithm="ES256",
            headers={"kid": self.key_id, "alg": "ES256"},
        )
        # 缓存到下次 REFRESH_INTERVAL 截止；提前 60s 失效避免边界冲突
        self._exp = now + self.REFRESH_INTERVAL - 60
        return self._token

    def force_refresh(self) -> str:
        """收到 403 InvalidProviderToken 时调，强制重签。"""
        self._exp = 0.0
        return self.token()


# ── 客户端 ──────────────────────────────────────────────────────────


def _host_for_env(env: str) -> str:
    """env → APNs HTTP/2 host。"""
    return (
        "api.sandbox.push.apple.com"
        if env == "sandbox"
        else "api.push.apple.com"
    )


class ApnsClient:
    """
    HTTP/2 客户端 + JWT 签发器。一个进程内单例足够；
    push.py 模块用 ``get_client()`` lazy 拿到。

    构造时即加载 .p8（构造失败 = 配置错误，应在启动阶段暴露）。
    实际网络请求只在 ``send_one`` / ``send_many`` 调用时发生。
    """

    def __init__(
        self,
        cfg: ApnsConfig,
        *,
        transport=None,   # 测试注入 httpx.MockTransport
    ) -> None:
        import httpx
        self.cfg = cfg
        self._jwt = _JwtSigner(cfg.key_path, cfg.key_id, cfg.team_id)
        client_kwargs: dict = {
            "http2": True,
            "timeout": cfg.request_timeout,
        }
        if transport is not None:
            client_kwargs["transport"] = transport
        self._client = httpx.AsyncClient(**client_kwargs)

    async def close(self) -> None:
        await self._client.aclose()

    # ── 单条 ────────────────────────────────────────────────────────

    async def send_one(
        self,
        *,
        device_token: str,
        env: str,
        payload: dict,
        collapse_id: str = "",
        priority: int = 10,
        push_type: str = "alert",
        expiration_seconds: int = 3600,
    ) -> ApnsResult:
        """发一条到指定设备；返回归一化结果。"""
        host = _host_for_env(env)
        url = f"https://{host}/3/device/{device_token}"
        headers = {
            "authorization": f"bearer {self._jwt.token()}",
            "apns-topic": self.cfg.topic,
            "apns-push-type": push_type,
            "apns-priority": str(priority),
            "apns-expiration": str(int(time.time()) + expiration_seconds),
        }
        if collapse_id:
            headers["apns-collapse-id"] = collapse_id[:64]  # APNs 上限
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        try:
            resp = await self._client.post(url, content=body, headers=headers)
        except Exception as e:
            logger.warning("APNs 请求异常 dev=%s: %s", device_token[:12], e)
            return ApnsResult(status=0, reason=f"client_error:{type(e).__name__}",
                              device=device_token)

        # 403 InvalidProviderToken → 强刷 JWT 重试一次
        if resp.status_code == 403:
            try:
                reason = resp.json().get("reason", "")
            except Exception:
                reason = ""
            if reason == "ExpiredProviderToken" or reason == "InvalidProviderToken":
                self._jwt.force_refresh()
                headers["authorization"] = f"bearer {self._jwt.token()}"
                try:
                    resp = await self._client.post(url, content=body, headers=headers)
                except Exception as e:
                    return ApnsResult(status=0, reason=f"retry_error:{type(e).__name__}",
                                      device=device_token)

        return self._build_result(resp, device_token)

    def _build_result(self, resp, device_token: str) -> ApnsResult:
        apns_id = resp.headers.get("apns-id", "")
        if resp.status_code == 200:
            return ApnsResult(status=200, reason="OK", device=device_token,
                              apns_id=apns_id)
        try:
            reason = resp.json().get("reason", "")
        except Exception:
            reason = resp.text[:80]
        return ApnsResult(status=resp.status_code, reason=reason,
                          device=device_token, apns_id=apns_id)

    # ── 批量（HTTP/2 多路复用并发） ─────────────────────────────────

    async def send_many(
        self,
        targets: list[dict],
        *,
        payload_factory=None,
        **single_kwargs,
    ) -> list[ApnsResult]:
        """
        并发推送多台设备。

        targets : 每项 dict 至少含 device_token / env，可含 payload override。
        payload_factory : 可选，``fn(target) -> payload`` 用于 per-device 文案。
                          缺省走 single_kwargs["payload"]。
        """
        import asyncio
        sem = asyncio.Semaphore(self.cfg.concurrency)

        async def _one(t: dict) -> ApnsResult:
            async with sem:
                p = payload_factory(t) if payload_factory else single_kwargs["payload"]
                kwargs = dict(single_kwargs)
                kwargs["payload"] = p
                kwargs["device_token"] = t["device_token"]
                kwargs["env"] = t.get("env", self.cfg.env_default)
                return await self.send_one(**kwargs)

        return await asyncio.gather(*[_one(t) for t in targets])
