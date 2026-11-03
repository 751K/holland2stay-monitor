"""设备平台 → 推送通道（APNs / FCM）的分流规则。

注册设备（Web 层 ``app.services.device_service``）和发推送（monitor 进程的
``mcore.push``）都要用同一条规则。放在这里而不是 Web 层，monitor 就不必为一个
判断函数去 import ``app.*``。
"""
from __future__ import annotations


#: 允许注册的平台。不认识的值在注册时就 400——否则传什么字符串就存什么，下面的
#: 分流白名单无从谈起。iOS 客户端不发这个字段，默认值是 ``ios``。
VALID_PLATFORMS = {"ios", "android", "macos"}
#: 走 APNs 的平台。macOS 客户端与 iOS 同走 APNs（同一个 APNS_TOPIC）。
APNS_PLATFORMS = {"ios", "macos"}
#: 走 FCM 的平台。
FCM_PLATFORMS = {"android"}


def push_channel(platform: str | None) -> str | None:
    """这个平台的设备走哪条推送通道：``"apns"``、``"fcm"``，或 ``None``（不推）。

    分流的唯一出处。原来四处各写一份，写法还不一样——``/devices/test`` 用
    ``in ("ios",)``，``mcore/push.py`` 和管理员测试推送用 ``!= "android"``。结果是
    macOS 设备在前者两边都不走，在后者被当成 iOS；而没有任何测试检查按平台分流，
    所以一直是绿的。

    **白名单**：认识的平台才推，未知值返回 ``None``。黑名单（``!= "android"``）会把
    任何拼错的字符串静默送去 APNs。空值按 ``ios``：老数据和 iOS 客户端都不发这个字段。
    """
    p = (platform or "ios").strip().lower()
    if p in APNS_PLATFORMS:
        return "apns"
    if p in FCM_PLATFORMS:
        return "fcm"
    return None
