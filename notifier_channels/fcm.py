"""
FCM HTTP v1 客户端
====================

职责
----
对接 Firebase Cloud Messaging HTTP v1 API：
- 用 service account JSON 密钥签 OAuth2 JWT → 换取 access token（缓存至过期）
- 用 httpx 客户端把 push payload POST 到 ``/v1/projects/{project_id}/messages:send``
- 解析 FCM 的 HTTP 响应，归一化成 FcmResult

设计原则
--------
- **纯 IO 层**：不读 Storage、不知 user/listing 语义，仅收发字节。
  上层 mcore/push.py 负责调度与节流。
- **无副作用 import**：构造 FcmClient 时才读 JSON / cryptography / JWT 库；
  monitor 进程在 FCM_ENABLED=false 时根本不会构造客户端。
- **失败 fail-closed**：任一异常路径都返回 ``FcmResult(status=0, reason=...)``，
  不抛到调用方。

与 APNs 的对应关系
------------------
- ApnsConfig / ApnsClient / ApnsResult / _JwtSigner / send_one / send_many
- FcmConfig / FcmClient / FcmResult / _AccessTokenCache / send_one / send_many

Payload 约定
------------
FCM 使用 **data-only** 消息（无 ``notification`` 字段）：
- Android FcmService.onMessageReceived 始终被调用（前后台均如此）
- 由客户端代码统一创建展示通知 + deep link PendingIntent
- 提供高可靠性：即使用户关掉应用，data-only 消息也会唤醒 doze 模式下的设备

data payload 字段（客户端消费）：
- title / body : 通知展示文案
- listing_id    : 导航锚点
- kind          : "new" | "status_change" | "booked" | "round" | "error"
- deep_link     : h2smonitor://listing/<id>
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _safe_int(val: str, default: int) -> int:
    try:
        return int(val)
    except (ValueError, TypeError):
        logger.warning("FCM 配置值 %r 不是合法整数，回退到 %d", val, default)
        return default


def _safe_float(val: str, default: float) -> float:
    try:
        return float(val)
    except (ValueError, TypeError):
        logger.warning("FCM 配置值 %r 不是合法浮点数，回退到 %.1f", val, default)
        return default


# ── 配置 ────────────────────────────────────────────────────────────


@dataclass
class FcmConfig:
    """FCM 客户端的运行时配置；通常从 .env 装填。"""

    project_id: str          # Firebase project ID
    client_email: str        # service account client_email
    private_key: str         # PEM-encoded RSA private key
    token_uri: str = "https://oauth2.googleapis.com/token"
    concurrency: int = 16
    request_timeout: float = 10.0

    @classmethod
    def from_env(cls) -> Optional["FcmConfig"]:
        """
        从环境变量读配置；FCM_ENABLED!=true 或缺关键字段时返回 None。
        优先走 FCM_SERVICE_ACCOUNT_PATH JSON 文件；文件不存在时 fallback 单项变量。
        """
        if os.environ.get("FCM_ENABLED", "").lower() != "true":
            return None
        path = os.environ.get("FCM_SERVICE_ACCOUNT_PATH", "").strip()
        if path and Path(path).exists():
            return cls._from_service_account(path)
        return cls._from_env_vars()

    @classmethod
    def _from_service_account(cls, path: str) -> Optional["FcmConfig"]:
        try:
            with open(path, "r") as fh:
                sa = json.load(fh)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("读取 FCM service account JSON 失败 %s: %s", path, e)
            return None
        missing = [k for k in ("project_id", "client_email", "private_key")
                   if not sa.get(k)]
        if missing:
            logger.warning("FCM service account JSON 缺少字段: %s", missing)
            return None
        try:
            return cls(
                project_id=sa["project_id"],
                client_email=sa["client_email"],
                private_key=sa["private_key"],
                token_uri=sa.get("token_uri", "https://oauth2.googleapis.com/token"),
                concurrency=_safe_int(os.environ.get("FCM_CONCURRENCY", "16") or "16", 16),
                request_timeout=_safe_float(
                    os.environ.get("FCM_REQUEST_TIMEOUT", "10") or "10", 10.0,
                ),
            )
        except Exception:
            logger.warning(
                "FCM 客户端构造失败 (project_id=%s client_email=%s)",
                sa.get("project_id", "?"), sa.get("client_email", "?"),
            )
            return None

    @classmethod
    def _from_env_vars(cls) -> Optional["FcmConfig"]:
        pid = os.environ.get("FCM_PROJECT_ID", "").strip()
        email = os.environ.get("FCM_CLIENT_EMAIL", "").strip()
        key = os.environ.get("FCM_PRIVATE_KEY", "").strip()
        missing = [n for n, v in (
            ("FCM_PROJECT_ID", pid), ("FCM_CLIENT_EMAIL", email),
            ("FCM_PRIVATE_KEY", key),
        ) if not v]
        if missing:
            logger.warning(
                "FCM_ENABLED=true 但缺少配置 %s，本进程禁用 FCM", missing,
            )
            return None
        return cls(
            project_id=pid,
            client_email=email,
            private_key=key,
            concurrency=int(os.environ.get("FCM_CONCURRENCY", "16") or 16),
            request_timeout=float(
                os.environ.get("FCM_REQUEST_TIMEOUT", "10") or 10,
            ),
        )


# ── 结果 ────────────────────────────────────────────────────────────


@dataclass
class FcmResult:
    """单条推送的归一化结果。

    status     : HTTP 状态码；客户端异常时为 0
    reason     : FCM JSON error 的 message 字段；无网络时为本地描述
    device     : 原始 FCM token，便于上层 disable 时定位
    message_id : FCM 服务端给的消息 ID（在响应 JSON 的 name 字段里）
    """

    status: int
    reason: str
    device: str
    message_id: str = ""

    @property
    def ok(self) -> bool:
        return self.status == 200

    #: FCM v1 里代表「这个 token 已经没用了」的机器可读错误码。
    #:
    #: 取自 ``error.details[].errorCode``（``type.googleapis.com/
    #: google.firebase.fcm.v1.FcmError``），拿不到时退回 ``error.status``。
    #: **不能拿 ``error.message`` 比**——那是给人看的散文，例如
    #: 「The registration token is not a valid FCM registration token」，
    #: 和这里任何一个常量都不相等，于是这个判定**从来没有命中过**，失效 token
    #: 永远不会被清理，每轮都对着墓碑重发一次。
    DEAD_CODES = frozenset({
        "UNREGISTERED",
        "INVALID_ARGUMENT",
        "SENDER_ID_MISMATCH",
    })

    @property
    def device_dead(self) -> bool:
        """需要把对应 device token 软停的状态。

        判据是 ``reason``（现在装的是 errorCode / status，不是 message）加上
        HTTP 状态。两者都要：``INVALID_ARGUMENT`` 也可能是我们自己 payload 写错，
        那时状态码同样是 400——但那种情况下每台设备都会 400，而不是某一台。
        软停单台设备是可逆的（客户端下次上报 token 会复活），所以这里取宽。
        """
        return self.status in (400, 404) and self.reason.upper() in self.DEAD_CODES


# ── OAuth2 Token 管理 ────────────────────────────────────────────────


class _AccessTokenCache:
    """Service account OAuth2 JWT → access token 缓存（按 exp 提前 60s 刷新）。"""

    SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
    REFRESH_INTERVAL = 55 * 60  # 55 min, token 有效期通常 1 hour

    def __init__(self, client_email: str, private_key_pem: str, token_uri: str) -> None:
        import jwt as _jwt
        from cryptography.hazmat.primitives.serialization import (
            load_pem_private_key,
        )
        self._client_email = client_email
        self._key = load_pem_private_key(
            private_key_pem.encode("utf-8"), password=None,
        )
        self._token_uri = token_uri
        self._access_token: str = ""
        self._exp: float = 0.0
        self._jwt = _jwt

    def token(self, now: Optional[float] = None) -> str:
        now = now if now is not None else time.time()
        if now < self._exp and self._access_token:
            return self._access_token

        assertion = self._make_assertion(now)
        self._access_token = self._exchange(assertion)
        self._exp = now + self.REFRESH_INTERVAL - 60
        return self._access_token

    def force_refresh(self) -> str:
        self._exp = 0.0
        return self.token()

    def _make_assertion(self, now: float) -> str:
        iat = int(now)
        payload = {
            "iss": self._client_email,
            "scope": self.SCOPE,
            "aud": self._token_uri,
            "iat": iat,
            "exp": iat + 3600,
        }
        return self._jwt.encode(
            payload, self._key, algorithm="RS256",
            headers={"alg": "RS256", "typ": "JWT"},
        )

    def _exchange(self, assertion: str) -> str:
        import httpx

        from net import direct_httpx_kwargs

        # 换 token 同样不能走抓取代理。这里是漏网的最后一处：send_one /
        # send_many 用的 AsyncClient 早就走了 direct_httpx_kwargs，唯独这次换
        # token 用的是 httpx 的**模块级函数**，它自带 trust_env=True，于是照旧
        # 从环境读 HTTPS_PROXY。
        #
        # 后果是整条 Android 推送链路一起挂：token 拿不到，send_many 里每个设备
        # 都抛同一个异常。2026-08-28 一次公告群发实测——代理账户欠费返回 402，
        # FCM 全军覆没，而同一批 APNs 正常送达（它没有这个漏洞）。
        resp = httpx.post(
            self._token_uri,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
            timeout=10.0,
            **direct_httpx_kwargs(),
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"FCM OAuth2 token exchange failed: {resp.status_code} {resp.text[:200]}"
            )
        return str(resp.json()["access_token"])


# ── 客户端 ──────────────────────────────────────────────────────────


def _build_url(project_id: str) -> str:
    return f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"


class FcmClient:
    """
    OAuth2 HTTP 客户端。一个进程内单例足够；
    push.py 用 ``get_fcm_client()`` lazy 拿到。

    构造时即加载 service account 密钥（构造失败 = 配置错误，应在启动阶段暴露）。
    """

    def __init__(
        self,
        cfg: FcmConfig,
        *,
        transport=None,  # 测试注入 httpx.MockTransport
    ) -> None:
        import httpx

        from net import direct_httpx_kwargs
        self.cfg = cfg
        self._auth = _AccessTokenCache(
            cfg.client_email, cfg.private_key, cfg.token_uri,
        )
        client_kwargs: dict = {
            "timeout": cfg.request_timeout,
        }
        if transport is not None:
            client_kwargs["transport"] = transport
        self._client = httpx.AsyncClient(**direct_httpx_kwargs(**client_kwargs))

    async def close(self) -> None:
        await self._client.aclose()

    # ── 单条 ──────────────────────────────────────────────────────

    async def send_one(
        self,
        *,
        device_token: str,
        payload: dict,
        collapse_key: str = "",
    ) -> FcmResult:
        """发一条到指定 FCM token；返回归一化结果。"""
        url = _build_url(self.cfg.project_id)
        # 取 token 必须在 try **里面**，而且不能直接调——``_auth.token()`` 在缓存
        # 过期时会走同步 httpx 去换 token，**阻塞事件循环**（整个进程的 asyncio
        # 都停在这一行上，包括其余渠道的推送和 web 的 SSE）。
        #
        # 放在 try 外面的后果更直接：换 token 一失败，异常穿出 send_one，而
        # send_many 的 gather 没有 return_exceptions，**整批 Android 推送一起丢**。
        # 2026-08-28 实测过一次：代理欠费 402，FCM 全军覆没而 APNs 正常。
        try:
            token = await asyncio.to_thread(self._auth.token)
        except Exception as e:
            logger.warning("FCM 取 access token 失败 dev=%s: %s",
                           device_token[:12], e)
            return FcmResult(
                status=0, reason=f"auth_error:{type(e).__name__}",
                device=device_token,
            )
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
        }

        message = dict(payload.get("message", {}))
        message["token"] = device_token
        if collapse_key:
            message.setdefault("android", {})["collapse_key"] = collapse_key[:64]

        body = json.dumps({"message": message}, ensure_ascii=False).encode("utf-8")

        try:
            resp = await self._client.post(url, content=body, headers=headers)
        except Exception as e:
            logger.warning("FCM 请求异常 dev=%s: %s", device_token[:12], e)
            return FcmResult(
                status=0, reason=f"client_error:{type(e).__name__}",
                device=device_token,
            )

        # 401 Unauthorized → token 过期，强刷重试一次
        if resp.status_code == 401:
            try:
                fresh = await asyncio.to_thread(self._auth.force_refresh)
                headers["Authorization"] = f"Bearer {fresh}"
                resp = await self._client.post(url, content=body, headers=headers)
            except Exception as e:
                return FcmResult(
                    status=0, reason=f"retry_error:{type(e).__name__}",
                    device=device_token,
                )

        return self._build_result(resp, device_token)

    def _build_result(self, resp, device_token: str) -> FcmResult:
        if resp.status_code == 200:
            try:
                name = resp.json().get("name", "")
            except Exception:
                name = ""
            return FcmResult(
                status=200, reason="OK", device=device_token,
                message_id=name,
            )
        try:
            error = resp.json().get("error", {}) or {}
            # 优先 details[].errorCode（FcmError），其次 error.status。
            # message 只留给日志——它是散文，拿来做判定永远不会相等。
            reason = ""
            for d in error.get("details") or []:
                if isinstance(d, dict) and d.get("errorCode"):
                    reason = str(d["errorCode"])
                    break
            if not reason:
                reason = str(error.get("status") or "")
            if not reason:
                reason = str(error.get("message") or "")[:80]
        except Exception:
            reason = resp.text[:80]
        return FcmResult(
            status=resp.status_code, reason=reason, device=device_token,
        )

    # ── 批量（HTTP/2 多路复用并发） ────────────────────────────────

    async def send_many(
        self,
        targets: list[dict],
        *,
        payload_factory=None,
        **single_kwargs,
    ) -> list[FcmResult]:
        """
        并发推送多台设备。

        targets : 每项 dict 至少含 device_token。
        payload_factory : 可选，``fn(target) -> payload`` 用于 per-device 文案。
        """
        import asyncio
        sem = asyncio.Semaphore(self.cfg.concurrency)

        async def _one(t: dict) -> FcmResult:
            async with sem:
                p = (
                    payload_factory(t)
                    if payload_factory
                    else single_kwargs["payload"]
                )
                kwargs = dict(single_kwargs)
                kwargs["payload"] = p
                kwargs["device_token"] = t["device_token"]
                return await self.send_one(**kwargs)

        # **return_exceptions=True 不可省。** 少了它，任何一台设备抛异常都会让
        # 整批的结果一起丢——包括那些本来已经发成功的。归一化成 FcmResult 之后
        # 上层的失效清理、重试、统计才都还能正常工作。
        raw = await asyncio.gather(*[_one(t) for t in targets],
                                   return_exceptions=True)
        out: list[FcmResult] = []
        for t, r in zip(targets, raw):
            if isinstance(r, BaseException):
                logger.warning("FCM 单设备异常 dev=%s: %s",
                               str(t.get("device_token", ""))[:12], r)
                out.append(FcmResult(
                    status=0, reason=f"task_error:{type(r).__name__}",
                    device=t.get("device_token", ""),
                ))
            else:
                out.append(r)
        return out
