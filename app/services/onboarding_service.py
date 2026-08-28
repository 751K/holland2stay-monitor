"""
新用户引导：算出「这个账号现在到底能不能收到通知」
==================================================

为什么需要这个模块
------------------
2026-08-28 线上 61 个注册用户里，只有 29 个真的能收到任何通知。更刺眼的是
中间那一档：21 个人设过筛选条件（有人设了 9 项），其中 7 个既没有设备也没有
外部渠道——他们做完了最麻烦的一步，然后什么都没发生，而界面上没有任何地方
告诉他们这件事。

面板上的通知铃铛帮不上忙，反而误导：那是 notifier.WebNotifier 写的全局流水，
不分用户、不过滤。用户设完筛选回到面板看见铃铛里有东西，会以为生效了。

判据
----
``reachable``（此刻能收到通知）= 三个条件同时成立：

1. ``user.enabled``               账号本身没被停用
2. ``user.notifications_enabled`` 通知总开关开着
3. 至少一条投递路径：有活跃设备（APNs / FCM）**或**配了外部渠道

第 3 条里的「有活跃设备」直接调 storage.get_active_devices_for_user——和真正
决定发不发的是同一段代码。抄一份 WHERE 出来迟早会跟它分叉。

筛选条件为什么不算进 reachable
------------------------------
筛选为空是合法状态，意思是「什么都推给我」，不是「没配好」。把它算成未完成
会对着一半只想随便看看的用户天天报警。它单独作为一条灰色提示出现，措辞是
「当前会推送全部房源」而不是「你还没设置」。
"""
from __future__ import annotations

import dataclasses
import logging

logger = logging.getLogger(__name__)

# notification_channels 里的值 → 界面上显示的名字
_CHANNEL_LABELS = {
    "imessage": "iMessage",
    "telegram": "Telegram",
    "email":    "Email",
    "whatsapp": "WhatsApp",
}


def _active_device_count(storage, user_id: str) -> int:
    """该用户此刻可推送的设备数。查不出来时按 0——引导页宁可多提示一句，
    也不能因为一次查询失败就告诉用户「你配好了」。"""
    try:
        return len(storage.get_active_devices_for_user(user_id))
    except Exception:
        logger.warning("查询活跃设备失败 user_id=%s", user_id, exc_info=True)
        return 0


def _filter_field_count(user) -> int:
    """筛选条件里填了几项。"""
    lf = getattr(user, "listing_filter", None)
    if lf is None:
        return 0
    try:
        return sum(1 for f in dataclasses.fields(lf)
                   if getattr(lf, f.name) not in (None, [], "", {}))
    except TypeError:
        return 0


def route_labels(state: dict, lang: str = "zh") -> list[str]:
    """把 routes 翻成界面上显示的名字。

    推送那一条要带设备数——「iPhone 推送」和「iPhone 推送（2 台）」对用户是
    不同的信息：他换过手机、旧设备还挂着的时候，这个数字是唯一的线索。
    """
    out: list[str] = []
    for r in state.get("routes", []):
        if r == "push":
            n = state.get("device_count", 0)
            base = "设备推送" if lang == "zh" else "Device push"
            out.append(f"{base}（{n}）" if n > 1 else base)
        else:
            out.append(_CHANNEL_LABELS.get(r, r))
    return out


def delivery_state(storage, user, lang: str = "zh") -> dict:
    """算出一个用户的投递状态，供引导清单和横幅使用。

    Returns
    -------
    dict，键的含义：

    ``reachable``      此刻能不能收到通知
    ``blocked_by``     不能收到的原因："account" / "toggle" / "no_route"；能收到时为 None
    ``routes``         投递方式的机器名，如 ["push", "telegram"]
    ``route_labels``   同上，翻成界面文案，如 ["设备推送（2）", "Telegram"]
    ``device_count``   活跃设备数
    ``filter_count``   筛选条件填了几项
    ``filter_empty``   筛选是否为空（合法状态，不算未完成）
    ``done``           引导是否可以收起来：能收到通知就算完成
    """
    device_count = _active_device_count(storage, user.id)
    channels = [c.strip().lower() for c in (user.notification_channels or [])
                if c and c.strip()]

    routes: list[str] = []
    if device_count:
        routes.append("push")
    routes.extend(channels)

    has_route = bool(routes)
    enabled = bool(getattr(user, "enabled", True))
    toggle = bool(getattr(user, "notifications_enabled", True))

    if not enabled:
        blocked_by = "account"
    elif not toggle:
        blocked_by = "toggle"
    elif not has_route:
        blocked_by = "no_route"
    else:
        blocked_by = None

    filter_count = _filter_field_count(user)

    state = {
        "reachable":    blocked_by is None,
        "blocked_by":   blocked_by,
        "routes":       routes,
        "device_count": device_count,
        "filter_count": filter_count,
        "filter_empty": filter_count == 0,
        "done":         blocked_by is None,
    }
    state["route_labels"] = route_labels(state, lang)
    return state
