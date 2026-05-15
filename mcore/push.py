"""
mcore.push — APNs 推送调度
============================

从 monitor.py / notifier.py 的"知道哪个用户匹配了哪条房源"语义，
转化为"应该推哪些 device_token、推什么内容"。

设计要点
--------
1. **APNs 故障不阻塞其他渠道**：所有公开函数都吞异常，写日志后返回，
   不抛到调用方（monitor 的 fire-and-forget）。
2. **节流去重**（防刷屏）：
   - 同一 (user_id, listing_id, kind) 5 分钟内最多 1 条
   - 同一 user_id 1 分钟内最多 10 条；超出聚合为 round
3. **APNs 未启用时所有调用 no-op**：返回 0 个发送，不开网络连接。
4. **运行时单例 ApnsClient**：第一次调用时构造（含 .p8 加载），
   后续复用；同进程内多协程并发安全。
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections import defaultdict, deque
from typing import Optional, Sequence

from notifier_channels.apns import (
    ApnsClient,
    ApnsConfig,
    ApnsResult,
)

logger = logging.getLogger(__name__)


# ── 单例客户端 ──────────────────────────────────────────────────────


_client_lock = threading.Lock()
_client: Optional[ApnsClient] = None
_client_disabled = False   # from_env 返回 None 或构造失败时为 True


def get_client() -> Optional[ApnsClient]:
    """惰性构造。返回 None = APNs 未启用（调用方应跳过）。"""
    global _client, _client_disabled
    if _client_disabled:
        return None
    if _client is not None:
        return _client
    with _client_lock:
        if _client_disabled:
            return None
        if _client is not None:
            return _client
        cfg = ApnsConfig.from_env()
        if cfg is None:
            _client_disabled = True
            return None
        try:
            _client = ApnsClient(cfg)
            logger.info("APNs 已启用 (topic=%s, env=%s)", cfg.topic, cfg.env_default)
            return _client
        except Exception:
            logger.exception("APNs 客户端初始化失败，禁用本进程推送")
            _client_disabled = True
            return None


def set_client(client: Optional[ApnsClient]) -> None:
    """供测试注入；生产代码不要调。"""
    global _client, _client_disabled
    with _client_lock:
        _client = client
        _client_disabled = client is None


def reset() -> None:
    """测试用：清空单例 + 节流状态。"""
    global _client, _client_disabled
    with _client_lock:
        _client = None
        _client_disabled = False
    _dedup.clear()
    for q in _per_user.values():
        q.clear()
    _per_user.clear()


# ── 节流去重 ────────────────────────────────────────────────────────


_DEDUP_WINDOW = 5 * 60           # 同一 (user, listing, kind) 5min 内仅 1 条
_PER_USER_LIMIT = 10             # 1 分钟内最多 10 条
_PER_USER_WINDOW = 60.0
_AGGREGATE_THRESHOLD = 3         # 一轮匹配 ≥3 套 → 聚合


# (user_id, listing_id, kind) -> last_sent_monotonic
_dedup: dict[tuple[str, str, str], float] = {}
# user_id -> deque[monotonic timestamps]
_per_user: dict[str, deque] = defaultdict(deque)
_state_lock = threading.Lock()


def _allow_send(user_id: str, listing_id: str, kind: str) -> bool:
    """同时检查 dedup + per-user rate；返回 True 表示放行。"""
    now = time.monotonic()
    key = (user_id, listing_id, kind)
    with _state_lock:
        last = _dedup.get(key)
        if last is not None and now - last < _DEDUP_WINDOW:
            return False
        q = _per_user[user_id]
        # 滑动窗口
        while q and now - q[0] > _PER_USER_WINDOW:
            q.popleft()
        if len(q) >= _PER_USER_LIMIT:
            return False
        _dedup[key] = now
        q.append(now)
        return True


# ── Payload 构造 ────────────────────────────────────────────────────


def _trim(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def _payload_new_listing(listing) -> dict:
    """单条新房源。"""
    city = listing.city or "新城市"
    price = getattr(listing, "price_display", "") or "?"
    area = ""
    try:
        fm = listing.feature_map()
        area = fm.get("area", "")
    except Exception:
        pass
    body_parts = [listing.status, f"{price}/月"]
    if area:
        body_parts.append(area)
    if listing.available_from:
        body_parts.append(f"{listing.available_from} 入住")
    body = " · ".join(body_parts)
    return {
        "aps": {
            "alert": {
                "title": _trim(f"🏠 {city} 新房源", 64),
                "body": _trim(body, 180),
            },
            "sound": "default",
            "thread-id": "listings",
            "mutable-content": 1,
        },
        "kind": "new",
        "listing_id": listing.id,
        "deep_link": f"h2smonitor://listing/{listing.id}",
    }


def _payload_status_change(listing, old_status: str, new_status: str) -> dict:
    return {
        "aps": {
            "alert": {
                "title": _trim(f"🔄 {listing.name}", 64),
                "body": _trim(f"{old_status} → {new_status}", 180),
            },
            "sound": "default",
            "thread-id": "listings",
        },
        "kind": "status_change",
        "listing_id": listing.id,
        "deep_link": f"h2smonitor://listing/{listing.id}",
    }


def _payload_booked(listing) -> dict:
    return {
        "aps": {
            "alert": {
                "title": "✅ 预订成功",
                "body": _trim(f"{listing.name} 已加入购物车，请尽快支付", 180),
            },
            "sound": "default",
            "thread-id": "booking",
        },
        "kind": "booked",
        "listing_id": listing.id,
        "deep_link": f"h2smonitor://listing/{listing.id}",
    }


def _payload_round_aggregate(listings, round_id: str) -> dict:
    """≥3 套聚合，避免锁屏刷屏。"""
    by_city: dict[str, int] = defaultdict(int)
    for l in listings:
        by_city[l.city or "?"] += 1
    parts = [f"{city} {cnt}" for city, cnt in sorted(by_city.items(), key=lambda kv: -kv[1])]
    body = " · ".join(parts) + " · 点开查看"
    return {
        "aps": {
            "alert": {
                "title": _trim(f"🏠 本轮 {len(listings)} 套新房源", 64),
                "body": _trim(body, 180),
            },
            "sound": "default",
            "thread-id": "listings",
        },
        "kind": "round",
        "round_id": round_id,
    }


def _payload_error(text: str, kind: str = "blocked") -> dict:
    return {
        "aps": {
            "alert": {
                "title": "⚠️ 监控异常",
                "body": _trim(text, 180),
            },
            "sound": "default",
            "thread-id": "errors",
        },
        "kind": kind,
    }


# ── 发送 ────────────────────────────────────────────────────────────


async def _send_to_user(
    storage,
    user_id: str,
    payload: dict,
    *,
    collapse_id: str = "",
) -> list[ApnsResult]:
    """
    取出 user 当前所有活跃设备，并发推；按 result.device_dead 软停设备。
    返回 ApnsResult 列表（空 = 没设备 / APNs 未启用）。
    """
    client = get_client()
    if client is None:
        return []
    try:
        devices = storage.get_active_devices_for_user(user_id)
    except Exception:
        logger.exception("get_active_devices_for_user 失败 user_id=%s", user_id)
        return []
    if not devices:
        return []

    targets = [{"device_token": d["device_token"], "env": d["env"]} for d in devices]
    results: list[ApnsResult] = []
    try:
        results = await client.send_many(
            targets,
            payload=payload,
            collapse_id=collapse_id,
        )
    except Exception:
        logger.exception("APNs send_many 异常 user_id=%s", user_id)
        return []

    # 后处理：disable 失活设备
    token_to_id = {d["device_token"]: d["id"] for d in devices}
    for r in results:
        if r.device_dead:
            did = token_to_id.get(r.device)
            if did is not None:
                try:
                    storage.disable_device(did, reason=r.reason)
                    logger.info(
                        "APNs 410/400 device disabled: id=%d reason=%s",
                        did, r.reason,
                    )
                except Exception:
                    logger.exception("disable_device 失败 id=%s", did)
        elif not r.ok:
            logger.warning(
                "APNs 失败 user_id=%s dev=%s status=%d reason=%s",
                user_id, r.device[:12], r.status, r.reason,
            )
    return results


# ── 对外 API ────────────────────────────────────────────────────────


async def dispatch(storage, user, listing, *, kind: str = "new") -> int:
    """
    单条房源 APNs 推送入口（同一 listing × 同一 user 短期内只发一次）。

    参数
    ----
    storage : Storage 实例（mcore/push 不持有，由调用方传入 monitor 的实例）
    user    : UserConfig
    listing : models.Listing
    kind    : "new" / "status_change" / "booked" / "round" / "blocked"

    返回成功发送的设备数（0 = 没设备 / 被节流 / APNs 未启用 / 全失败）。
    """
    try:
        if not _allow_send(user.id, listing.id, kind):
            return 0
        if kind == "new":
            p = _payload_new_listing(listing)
        elif kind == "booked":
            p = _payload_booked(listing)
        else:
            # status_change 走 dispatch_status_change 路径；这里 fallback
            return 0
        results = await _send_to_user(storage, user.id, p)
        return sum(1 for r in results if r.ok)
    except Exception:
        logger.exception("push.dispatch 异常 user=%s listing=%s", user.id, listing.id)
        return 0


async def dispatch_status_change(storage, user, listing, old_status: str,
                                 new_status: str) -> int:
    try:
        if not _allow_send(user.id, listing.id, "status_change"):
            return 0
        p = _payload_status_change(listing, old_status, new_status)
        results = await _send_to_user(storage, user.id, p)
        return sum(1 for r in results if r.ok)
    except Exception:
        logger.exception("push.dispatch_status_change 异常 user=%s", user.id)
        return 0


async def dispatch_aggregate(
    storage, user, listings: Sequence, *, round_id: str,
) -> int:
    """≥3 套时上层调；同一 round_id collapse-id，覆盖之前的聚合。"""
    try:
        if not listings:
            return 0
        # 聚合也走 dedup（避免 round 在分钟内重复）
        if not _allow_send(user.id, round_id, "round"):
            return 0
        p = _payload_round_aggregate(listings, round_id)
        results = await _send_to_user(storage, user.id, p,
                                      collapse_id=round_id)
        return sum(1 for r in results if r.ok)
    except Exception:
        logger.exception("push.dispatch_aggregate 异常 user=%s", user.id)
        return 0


async def dispatch_error(
    storage, user, message: str, *, kind: str = "blocked",
) -> int:
    """403 屏蔽这类异常事件——上层 monitor.run_once 已有 30 分钟节流，
    这里再加 dedup 防多用户/多 round 重复。"""
    try:
        if not _allow_send(user.id, kind, kind):
            return 0
        p = _payload_error(message, kind=kind)
        results = await _send_to_user(storage, user.id, p)
        return sum(1 for r in results if r.ok)
    except Exception:
        logger.exception("push.dispatch_error 异常 user=%s", user.id)
        return 0


# ── 聚合判定 ────────────────────────────────────────────────────────


def should_aggregate(matched_count: int) -> bool:
    """匹配 ≥ 阈值时使用 round 聚合而不是逐条推。"""
    return matched_count >= _AGGREGATE_THRESHOLD


def aggregate_threshold() -> int:
    return _AGGREGATE_THRESHOLD
