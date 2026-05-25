"""
mcore.push — APNs + FCM 推送调度
==================================

从 monitor.py / notifier.py 的"知道哪个用户匹配了哪条房源"语义，
转化为"应该推哪些 device_token、推什么内容"。

设计要点
--------
1. **APNs / FCM 故障不阻塞其他渠道**：所有公开函数都吞异常，写日志后返回，
   不抛到调用方（monitor 的 fire-and-forget）。
2. **节流去重**（防刷屏）：
   - 同一 (user_id, listing_id, kind) 5 分钟内最多 1 条
   - 同一 user_id 1 分钟内最多 10 条；超出聚合为 round
3. **APNs / FCM 未启用时各自 no-op**：返回 0 个发送，不开网络连接。
4. **运行时单例客户端**：第一次调用时构造（含 .p8 / service account 加载），
   后续复用；同进程内多协程并发安全。
5. **平台分离**：按 device_tokens.platform 字段分流到 APNs（iOS）或 FCM（Android）。
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
from notifier_channels.fcm import (
    FcmClient,
    FcmConfig,
    FcmResult,
)

logger = logging.getLogger(__name__)


# ── 单例客户端 ──────────────────────────────────────────────────────


_client_lock = threading.Lock()
_client: Optional[ApnsClient] = None
_client_disabled = False   # from_env 返回 None 或构造失败时为 True
_client_retry_after = 0.0  # monotonic；避免配置临时缺失后永久 no-op
_DISABLED_RETRY_SECONDS = 60.0


def get_client() -> Optional[ApnsClient]:
    """惰性构造。返回 None = APNs 未启用（调用方应跳过）。"""
    global _client, _client_disabled, _client_retry_after
    if _client_disabled:
        if time.monotonic() < _client_retry_after:
            return None
    if _client is not None:
        return _client
    with _client_lock:
        if _client_disabled:
            if time.monotonic() < _client_retry_after:
                return None
        if _client is not None:
            return _client
        cfg = ApnsConfig.from_env()
        if cfg is None:
            _client_disabled = True
            _client_retry_after = time.monotonic() + _DISABLED_RETRY_SECONDS
            logger.info("APNs 未启用或配置不完整，%ds 后重试", _DISABLED_RETRY_SECONDS)
            return None
        try:
            _client = ApnsClient(cfg)
            _client_disabled = False
            _client_retry_after = 0.0
            logger.info("APNs 已启用 (topic=%s, env=%s)", cfg.topic, cfg.env_default)
            return _client
        except Exception:
            logger.exception("APNs 客户端初始化失败，%ds 后重试", _DISABLED_RETRY_SECONDS)
            _client_disabled = True
            _client_retry_after = time.monotonic() + _DISABLED_RETRY_SECONDS
            return None


def set_client(client: Optional[ApnsClient]) -> None:
    """供测试注入；生产代码不要调。"""
    global _client, _client_disabled, _client_retry_after
    with _client_lock:
        _client = client
        _client_disabled = client is None
        _client_retry_after = 0.0


def reset() -> None:
    """测试用：清空单例 + 节流状态。"""
    global _client, _client_disabled, _client_retry_after
    with _client_lock:
        _client = None
        _client_disabled = False
        _client_retry_after = 0.0
    global _fcm_client, _fcm_disabled, _fcm_retry_after
    with _fcm_lock:
        _fcm_client = None
        _fcm_disabled = False
        _fcm_retry_after = 0.0
    _dedup.clear()
    for q in _per_user.values():
        q.clear()
    _per_user.clear()


# ── FCM 单例客户端 ────────────────────────────────────────────────────

_fcm_lock = threading.Lock()
_fcm_client: Optional[FcmClient] = None
_fcm_disabled = False
_fcm_retry_after = 0.0


def get_fcm_client() -> Optional[FcmClient]:
    """惰性构造 FCM 客户端。返回 None = FCM 未启用（调用方应跳过）。"""
    global _fcm_client, _fcm_disabled, _fcm_retry_after
    if _fcm_disabled:
        if time.monotonic() < _fcm_retry_after:
            return None
    if _fcm_client is not None:
        return _fcm_client
    with _fcm_lock:
        if _fcm_disabled:
            if time.monotonic() < _fcm_retry_after:
                return None
        if _fcm_client is not None:
            return _fcm_client
        cfg = FcmConfig.from_env()
        if cfg is None:
            _fcm_disabled = True
            _fcm_retry_after = time.monotonic() + _DISABLED_RETRY_SECONDS
            logger.info("FCM 未启用或配置不完整，%ds 后重试", _DISABLED_RETRY_SECONDS)
            return None
        try:
            _fcm_client = FcmClient(cfg)
            _fcm_disabled = False
            _fcm_retry_after = 0.0
            logger.info("FCM 已启用 (project_id=%s)", cfg.project_id)
            return _fcm_client
        except Exception:
            logger.exception("FCM 客户端初始化失败，%ds 后重试", _DISABLED_RETRY_SECONDS)
            _fcm_disabled = True
            _fcm_retry_after = time.monotonic() + _DISABLED_RETRY_SECONDS
            return None


def set_fcm_client(client: Optional[FcmClient]) -> None:
    """供测试注入；生产代码不要调。"""
    global _fcm_client, _fcm_disabled, _fcm_retry_after
    with _fcm_lock:
        _fcm_client = client
        _fcm_disabled = client is None
        _fcm_retry_after = 0.0


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


# ── 翻译 ────────────────────────────────────────────────────────────

# 通知模板中英双语映射。key 为英文原文，value 为各语言翻译。
# 新增语言只需在此字典中添加对应条目。
_T = {
    # new listing
    "[{source}] {city} new listing": {
        "zh": "[{source}] {city} 新房源",
    },
    "this round {n} new listings": {
        "zh": "本轮 {n} 套新房源",
    },
    "{status} · {price}/mo": {
        "zh": "{status} · {price}/月",
    },
    "{date} move-in": {
        "zh": "{date} 入住",
    },
    # booked
    "Booking successful": {
        "zh": "预订成功",
    },
    "{name} added to cart, please pay promptly": {
        "zh": "{name} 已加入购物车，请尽快支付",
    },
    # round aggregate
    "tap to view": {
        "zh": "点开查看",
    },
    # error
    "Monitor error": {
        "zh": "监控异常",
    },
}


def _t(text: str, lang: str) -> str:
    """Translate *text* to *lang* if a mapping exists; otherwise return as-is."""
    if lang == "en" or not lang:
        return text
    entry = _T.get(text)
    if entry:
        return entry.get(lang, text)
    return text


# ── Payload 构造 ────────────────────────────────────────────────────


def _trim(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def _source_short(source: str | None) -> str:
    value = (source or "holland2stay").strip().lower()
    return {
        "holland2stay": "H2S",
        "ourdomain": "OD",
    }.get(value, value.upper() or "H2S")


def _payload_new_listing(listing, *, lang: str = "en") -> dict:
    """单条新房源。"""
    city = listing.city or "New City"
    source = _source_short(getattr(listing, "source", ""))
    price = getattr(listing, "price_display", "") or "?"
    area = ""
    try:
        fm = listing.feature_map()
        area = fm.get("area", "")
    except Exception:
        pass
    body_parts = [listing.status, _t("{status} · {price}/mo", lang).format(status=listing.status, price=price)]
    if area:
        body_parts.append(area)
    if listing.available_from:
        body_parts.append(_t("{date} move-in", lang).format(date=listing.available_from))
    body = " · ".join(body_parts)
    title_tmpl = _t("[{source}] {city} new listing", lang)
    return {
        "aps": {
            "alert": {
                "title": _trim(title_tmpl.format(source=source, city=city), 64),
                "body": _trim(body, 180),
            },
            "sound": "default",
            "thread-id": "listings",
            "mutable-content": 1,
        },
        "kind": "new",
        "listing_id": listing.id,
        "source": getattr(listing, "source", "") or "holland2stay",
        "deep_link": f"h2smonitor://listing/{listing.id}",
    }


def _payload_status_change(listing, old_status: str, new_status: str, *, lang: str = "en") -> dict:
    source = _source_short(getattr(listing, "source", ""))
    status_text = _t("{old} → {new}", lang).format(old=old_status, new=new_status)
    return {
        "aps": {
            "alert": {
                "title": _trim(f"[{source}] {listing.name}", 64),
                "body": _trim(status_text, 180),
            },
            "sound": "default",
            "thread-id": "listings",
        },
        "kind": "status_change",
        "listing_id": listing.id,
        "source": getattr(listing, "source", "") or "holland2stay",
        "deep_link": f"h2smonitor://listing/{listing.id}",
    }


def _payload_booked(listing, *, lang: str = "en") -> dict:
    source = _source_short(getattr(listing, "source", ""))
    return {
        "aps": {
            "alert": {
                "title": f"[{source}] {_t('Booking successful', lang)}",
                "body": _trim(_t("{name} added to cart, please pay promptly", lang).format(name=listing.name), 180),
            },
            "sound": "default",
            "thread-id": "booking",
        },
        "kind": "booked",
        "listing_id": listing.id,
        "source": getattr(listing, "source", "") or "holland2stay",
        "deep_link": f"h2smonitor://listing/{listing.id}",
    }


def _payload_round_aggregate(listings, round_id: str, *, lang: str = "en") -> dict:
    """≥3 套聚合，避免锁屏刷屏。"""
    by_city: dict[str, int] = defaultdict(int)
    by_source: dict[str, int] = defaultdict(int)
    for l in listings:
        by_city[l.city or "?"] += 1
        by_source[_source_short(getattr(l, "source", ""))] += 1
    parts = [f"{city} {cnt}" for city, cnt in sorted(by_city.items(), key=lambda kv: -kv[1])]
    sources = ", ".join(f"{source} {cnt}" for source, cnt in sorted(by_source.items()))
    body = f"{sources} · " + " · ".join(parts) + f" · {_t('tap to view', lang)}"
    title_tmpl = _t("this round {n} new listings", lang)
    return {
        "aps": {
            "alert": {
                "title": _trim(title_tmpl.format(n=len(listings)), 64),
                "body": _trim(body, 180),
            },
            "sound": "default",
            "thread-id": "listings",
        },
        "kind": "round",
        "round_id": round_id,
        "sources": dict(by_source),
    }


def _payload_error(text: str, kind: str = "blocked", *, lang: str = "en") -> dict:
    return {
        "aps": {
            "alert": {
                "title": _t("Monitor error", lang),
                "body": _trim(text, 180),
            },
            "sound": "default",
            "thread-id": "errors",
        },
        "kind": kind,
    }


# ── FCM Payload 构造 ─────────────────────────────────────────────────

# FCM 使用 data-only 消息：全部走 data payload，由 Android FcmService
# 统一创建展示通知 + PendingIntent deep link。


def _fcm_data(listing, listing_id: str, kind: str, deep_link: str,
              title: str, body: str) -> dict:
    """FCM data payload 公共字段。"""
    return {
        "title": _trim(title, 64),
        "body": _trim(body, 180),
        "listing_id": listing_id,
        "kind": kind,
        "deep_link": deep_link,
    }


def _fcm_payload_new_listing(listing, *, lang: str = "en") -> dict:
    city = listing.city or "New City"
    source = _source_short(getattr(listing, "source", ""))
    price = getattr(listing, "price_display", "") or "?"
    area = ""
    try:
        fm = listing.feature_map()
        area = fm.get("area", "")
    except Exception:
        pass
    body_parts = [listing.status,
                  _t("{status} · {price}/mo", lang).format(status=listing.status, price=price)]
    if area:
        body_parts.append(area)
    if listing.available_from:
        body_parts.append(_t("{date} move-in", lang).format(date=listing.available_from))
    body = " · ".join(body_parts)
    title = _t("[{source}] {city} new listing", lang).format(source=source, city=city)
    data = _fcm_data(listing, listing.id, "new",
                     f"h2smonitor://listing/{listing.id}", title, body)
    return {
        "message": {
            "data": data,
            "android": {
                "priority": "high",
                "collapse_key": ("new_" + listing.id)[:64],
            },
        },
    }


def _fcm_payload_status_change(listing, old_status: str, new_status: str,
                               *, lang: str = "en") -> dict:
    source = _source_short(getattr(listing, "source", ""))
    status_text = _t("{old} → {new}", lang).format(old=old_status, new=new_status)
    title = f"[{source}] {listing.name}"
    body = status_text
    data = _fcm_data(listing, listing.id, "status_change",
                     f"h2smonitor://listing/{listing.id}", title, body)
    return {
        "message": {
            "data": data,
            "android": {
                "priority": "high",
                "collapse_key": ("sc_" + listing.id)[:64],
            },
        },
    }


def _fcm_payload_booked(listing, *, lang: str = "en") -> dict:
    source = _source_short(getattr(listing, "source", ""))
    title = f"[{source}] {_t('Booking successful', lang)}"
    body = _t("{name} added to cart, please pay promptly", lang).format(
        name=listing.name,
    )
    data = _fcm_data(listing, listing.id, "booked",
                     f"h2smonitor://listing/{listing.id}", title, body)
    return {
        "message": {
            "data": data,
            "android": {
                "priority": "high",
                "collapse_key": ("booked_" + listing.id)[:64],
            },
        },
    }


def _fcm_payload_round_aggregate(listings, round_id: str, *, lang: str = "en") -> dict:
    by_city: dict[str, int] = defaultdict(int)
    by_source: dict[str, int] = defaultdict(int)
    for l in listings:
        by_city[l.city or "?"] += 1
        by_source[_source_short(getattr(l, "source", ""))] += 1
    parts = [f"{city} {cnt}" for city, cnt in sorted(by_city.items(), key=lambda kv: -kv[1])]
    sources = ", ".join(f"{source} {cnt}" for source, cnt in sorted(by_source.items()))
    body = f"{sources} · " + " · ".join(parts) + f" · {_t('tap to view', lang)}"
    title = _t("this round {n} new listings", lang).format(n=len(listings))
    title_trimmed = _trim(title, 64)
    return {
        "message": {
            "data": {
                "title": title_trimmed,
                "body": _trim(body, 180),
                "kind": "round",
                "round_id": round_id,
                "deep_link": "",
            },
            "android": {
                "priority": "high",
                "collapse_key": round_id[:64],
            },
        },
    }


def _fcm_payload_error(text: str, kind: str = "blocked", *, lang: str = "en") -> dict:
    title = _t("Monitor error", lang)
    return {
        "message": {
            "data": {
                "title": _trim(title, 64),
                "body": _trim(text, 180),
                "kind": kind,
                "deep_link": "",
            },
            "android": {
                "priority": "high",
                "collapse_key": kind[:64],
            },
        },
    }


# ── 发送 ────────────────────────────────────────────────────────────


async def _send_to_user(
    storage,
    user_id: str,
    payload_fn,          # callable(lang: str) -> dict
    *,
    collapse_id: str = "",
) -> list[ApnsResult]:
    """
    取出 user 当前所有活跃设备，按语言分组发送翻译后的 payload。

    *payload_fn* 接受语言代码（'en' | 'zh'）返回对应的 APNs payload dict。
    不传 payload_fn 时默认推英文。
    """
    client = get_client()
    if client is None:
        logger.warning("APNs 跳过：client 未启用或初始化失败 user_id=%s（检查 APNS_ENABLED / .p8 / APNS_* 环境变量）", user_id)
        return []
    try:
        devices = storage.get_active_devices_for_user(user_id)
    except Exception:
        logger.exception("get_active_devices_for_user 失败 user_id=%s", user_id)
        return []
    if not devices:
        logger.info("APNs 跳过：user_id=%s 没有活跃设备", user_id)
        return []

    # 分离 iOS / Android；未设 platform 的老数据默认走 APNs
    ios_devices = [d for d in devices if d.get("platform", "ios") != "android"]

    # 按语言分组（仅 iOS）
    by_lang: dict[str, list[dict]] = defaultdict(list)
    for d in ios_devices:
        lang = (d.get("language") or "en").strip().lower()[:8]
        by_lang[lang].append(d)

    if ios_devices:
        env_counts = dict(sorted(
            (env, sum(1 for d in ios_devices if d.get("env") == env))
            for env in {d.get("env", "") for d in ios_devices}
        ))
        logger.info(
            "APNs 准备发送 user_id=%s devices=%d envs=%s langs=%s",
            user_id, len(ios_devices), env_counts,
            {lang: len(ds) for lang, ds in by_lang.items()},
        )
    else:
        logger.info("APNs 跳过：user_id=%s 无 iOS 设备", user_id)
        env_counts = {}

    all_results: list[ApnsResult] = []
    for lang, lang_devices in by_lang.items():
        payload = payload_fn(lang) if callable(payload_fn) else payload_fn
        targets = [{"device_token": d["device_token"], "env": d["env"]} for d in lang_devices]
        try:
            results = await client.send_many(targets, payload=payload, collapse_id=collapse_id)
            all_results.extend(results)
        except Exception:
            logger.exception("APNs send_many 异常 user_id=%s lang=%s", user_id, lang)
            continue

    # 后处理：disable 失活设备
    token_to_id = {d["device_token"]: d["id"] for d in ios_devices}
    for r in all_results:
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
    return all_results


async def _send_fcm_to_user(
    storage,
    user_id: str,
    payload_fn,          # callable(lang: str) -> dict
    *,
    collapse_key: str = "",
) -> list[FcmResult]:
    """
    取出 user 当前所有 Android 设备，按语言分组发送 FCM data payload。
    """
    client = get_fcm_client()
    if client is None:
        logger.info("FCM 跳过：client 未启用 user_id=%s", user_id)
        return []
    try:
        devices = storage.get_active_devices_for_user(user_id)
    except Exception:
        logger.exception("get_active_devices_for_user 失败 user_id=%s", user_id)
        return []
    if not devices:
        logger.info("FCM 跳过：user_id=%s 没有活跃设备", user_id)
        return []

    # 只取 Android 设备（platform == "android"）
    android_devices = [d for d in devices if d.get("platform", "ios") == "android"]
    if not android_devices:
        logger.info("FCM 跳过：user_id=%s 没有 Android 设备", user_id)
        return []

    # 按语言分组
    by_lang: dict[str, list[dict]] = defaultdict(list)
    for d in android_devices:
        lang = (d.get("language") or "en").strip().lower()[:8]
        by_lang[lang].append(d)

    logger.info(
        "FCM 准备发送 user_id=%s devices=%d langs=%s",
        user_id, len(android_devices),
        {lang: len(ds) for lang, ds in by_lang.items()},
    )

    all_results: list[FcmResult] = []
    for lang, lang_devices in by_lang.items():
        payload = payload_fn(lang) if callable(payload_fn) else payload_fn
        targets = [{"device_token": d["device_token"]} for d in lang_devices]
        try:
            results = await client.send_many(
                targets, payload=payload, collapse_key=collapse_key,
            )
            all_results.extend(results)
        except Exception:
            logger.exception("FCM send_many 异常 user_id=%s lang=%s", user_id, lang)
            continue

    # 后处理：disable 失活设备
    token_to_id = {d["device_token"]: d["id"] for d in android_devices}
    for r in all_results:
        if r.device_dead:
            did = token_to_id.get(r.device)
            if did is not None:
                try:
                    storage.disable_device(did, reason=r.reason)
                    logger.info(
                        "FCM device disabled: id=%d reason=%s", did, r.reason,
                    )
                except Exception:
                    logger.exception("disable_device 失败 id=%s", did)
        elif not r.ok:
            logger.warning(
                "FCM 失败 user_id=%s dev=%s status=%d reason=%s",
                user_id, r.device[:12], r.status, r.reason,
            )
    return all_results


# ── 对外 API ────────────────────────────────────────────────────────


async def dispatch(storage, user, listing, *, kind: str = "new") -> int:
    """
    单条房源 APNs 推送入口（同一 listing × 同一 user 短期内只发一次）。

    参数
    ----
    storage : Storage 实例（mcore/push 不持有，由调用方传入 monitor 的实例）
    user    : UserConfig
    listing : models.Listing
    kind    : "new" / "status_change" / "booked"

    返回成功发送的设备数（0 = 没设备 / 被节流 / APNs 未启用 / 全失败）。
    """
    try:
        if not _allow_send(user.id, listing.id, kind):
            return 0
        if kind == "new":
            payload_fn = lambda lang: _payload_new_listing(listing, lang=lang)
            fcm_payload_fn = lambda lang: _fcm_payload_new_listing(listing, lang=lang)
        elif kind == "booked":
            payload_fn = lambda lang: _payload_booked(listing, lang=lang)
            fcm_payload_fn = lambda lang: _fcm_payload_booked(listing, lang=lang)
        else:
            return 0
        results = await _send_to_user(storage, user.id, payload_fn)
        fcm_results = await _send_fcm_to_user(storage, user.id, fcm_payload_fn)
        return sum(1 for r in results if r.ok) + sum(1 for r in fcm_results if r.ok)
    except Exception:
        logger.exception("push.dispatch 异常 user=%s listing=%s", user.id, listing.id)
        return 0


async def dispatch_status_change(storage, user, listing, old_status: str,
                                 new_status: str) -> int:
    try:
        if not _allow_send(user.id, listing.id, "status_change"):
            return 0
        payload_fn = lambda lang: _payload_status_change(listing, old_status, new_status, lang=lang)
        fcm_payload_fn = lambda lang: _fcm_payload_status_change(listing, old_status, new_status, lang=lang)
        results = await _send_to_user(storage, user.id, payload_fn)
        fcm_results = await _send_fcm_to_user(storage, user.id, fcm_payload_fn)
        return sum(1 for r in results if r.ok) + sum(1 for r in fcm_results if r.ok)
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
        if not _allow_send(user.id, round_id, "round"):
            return 0
        payload_fn = lambda lang: _payload_round_aggregate(listings, round_id, lang=lang)
        fcm_payload_fn = lambda lang: _fcm_payload_round_aggregate(listings, round_id, lang=lang)
        results = await _send_to_user(storage, user.id, payload_fn,
                                      collapse_id=round_id)
        fcm_results = await _send_fcm_to_user(storage, user.id, fcm_payload_fn,
                                              collapse_key=round_id)
        return sum(1 for r in results if r.ok) + sum(1 for r in fcm_results if r.ok)
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
        payload_fn = lambda lang: _payload_error(message, kind=kind, lang=lang)
        fcm_payload_fn = lambda lang: _fcm_payload_error(message, kind=kind, lang=lang)
        results = await _send_to_user(storage, user.id, payload_fn)
        fcm_results = await _send_fcm_to_user(storage, user.id, fcm_payload_fn)
        return sum(1 for r in results if r.ok) + sum(1 for r in fcm_results if r.ok)
    except Exception:
        logger.exception("push.dispatch_error 异常 user=%s", user.id)
        return 0


# ── Admin 推送 ────────────────────────────────────────────────────────


async def _send_to_admin(storage, payload_fn, *, collapse_id: str = "") -> list[ApnsResult]:
    """取出所有 admin 活跃设备，按语言分组发送。"""
    client = get_client()
    if client is None:
        logger.warning("APNs admin 跳过：client 未启用")
        return []
    try:
        devices = storage.get_active_devices_for_admin()
    except Exception:
        logger.exception("get_active_devices_for_admin 失败")
        return []
    if not devices:
        logger.info("APNs admin 跳过：没有活跃的 admin 设备")
        return []

    # 分离 iOS / Android
    ios_devices = [d for d in devices if d.get("platform", "ios") != "android"]

    # 按语言分组（仅 iOS）
    by_lang: dict[str, list[dict]] = defaultdict(list)
    for d in ios_devices:
        lang = (d.get("language") or "en").strip().lower()[:8]
        by_lang[lang].append(d)

    logger.info("APNs admin 准备发送 devices=%d langs=%s",
                len(ios_devices), {lang: len(ds) for lang, ds in by_lang.items()})

    all_results: list[ApnsResult] = []
    for lang, lang_devices in by_lang.items():
        payload = payload_fn(lang) if callable(payload_fn) else payload_fn
        targets = [{"device_token": d["device_token"], "env": d["env"]} for d in lang_devices]
        try:
            results = await client.send_many(targets, payload=payload, collapse_id=collapse_id)
            all_results.extend(results)
        except Exception:
            logger.exception("APNs admin send_many 异常 lang=%s", lang)
            continue

    token_to_id = {d["device_token"]: d["id"] for d in ios_devices}
    for r in all_results:
        if r.device_dead:
            did = token_to_id.get(r.device)
            if did is not None:
                try:
                    storage.disable_device(did, reason=r.reason)
                except Exception:
                    logger.exception("disable_device 失败 id=%s", did)
        elif not r.ok:
            logger.warning("APNs admin 失败 dev=%s status=%d reason=%s",
                           r.device[:12], r.status, r.reason)
    return all_results


async def _send_fcm_to_admin(storage, payload_fn, *, collapse_key: str = "") -> list[FcmResult]:
    """取出所有 admin Android 设备，按语言分组发送 FCM。"""
    client = get_fcm_client()
    if client is None:
        logger.info("FCM admin 跳过：client 未启用")
        return []
    try:
        devices = storage.get_active_devices_for_admin()
    except Exception:
        logger.exception("get_active_devices_for_admin 失败")
        return []
    if not devices:
        logger.info("FCM admin 跳过：没有活跃的 admin 设备")
        return []

    android_devices = [d for d in devices if d.get("platform", "ios") == "android"]
    if not android_devices:
        logger.info("FCM admin 跳过：没有 Android 设备")
        return []

    by_lang: dict[str, list[dict]] = defaultdict(list)
    for d in android_devices:
        lang = (d.get("language") or "en").strip().lower()[:8]
        by_lang[lang].append(d)

    logger.info("FCM admin 准备发送 devices=%d langs=%s",
                len(android_devices), {lang: len(ds) for lang, ds in by_lang.items()})

    all_results: list[FcmResult] = []
    for lang, lang_devices in by_lang.items():
        payload = payload_fn(lang) if callable(payload_fn) else payload_fn
        targets = [{"device_token": d["device_token"]} for d in lang_devices]
        try:
            results = await client.send_many(
                targets, payload=payload, collapse_key=collapse_key,
            )
            all_results.extend(results)
        except Exception:
            logger.exception("FCM admin send_many 异常 lang=%s", lang)
            continue

    token_to_id = {d["device_token"]: d["id"] for d in android_devices}
    for r in all_results:
        if r.device_dead:
            did = token_to_id.get(r.device)
            if did is not None:
                try:
                    storage.disable_device(did, reason=r.reason)
                except Exception:
                    logger.exception("disable_device 失败 id=%s", did)
        elif not r.ok:
            logger.warning("FCM admin 失败 dev=%s status=%d reason=%s",
                           r.device[:12], r.status, r.reason)
    return all_results


async def dispatch_admin(storage, message: str, *, kind: str = "blocked") -> int:
    """admin 设备推送入口（APNs + FCM）。dedup 按 (admin, kind) 粒度。"""
    try:
        if not _allow_send("__admin__", kind, kind):
            return 0
        payload_fn = lambda lang: _payload_error(message, kind=kind, lang=lang)
        fcm_payload_fn = lambda lang: _fcm_payload_error(message, kind=kind, lang=lang)
        results = await _send_to_admin(storage, payload_fn)
        fcm_results = await _send_fcm_to_admin(storage, fcm_payload_fn)
        return sum(1 for r in results if r.ok) + sum(1 for r in fcm_results if r.ok)
    except Exception:
        logger.exception("push.dispatch_admin 异常 kind=%s", kind)
        return 0


# ── 聚合判定 ────────────────────────────────────────────────────────


def should_aggregate(matched_count: int) -> bool:
    """匹配 ≥ 阈值时使用 round 聚合而不是逐条推。"""
    return matched_count >= _AGGREGATE_THRESHOLD


def aggregate_threshold() -> int:
    return _AGGREGATE_THRESHOLD
