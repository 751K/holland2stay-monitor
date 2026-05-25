"""
FlatRadar 公开支持页面文案。

⚠️ App Store Connect 提交时**必填的 Support URL** 指向 `/support`。
苹果审核员会点开验证页面真实存在、有人维护、能联系到开发者。

字段
----
- `SECTIONS_EN` / `SECTIONS_ZH`: 段落列表，每段一个 (title, body) 元组
- `CONTACT_EMAIL`: 用户/审核员唯一联系邮箱，可用 `SUPPORT_EMAIL` 覆盖

改动建议
--------
新增 FAQ 时 append 到 SECTIONS_*；如果 iOS 端有同款 in-app help 页，
也要同步那边的文案，避免出现 "App 内说 X，网页说 Y" 的不一致。
"""
from __future__ import annotations

import os


CONTACT_EMAIL = os.environ.get("SUPPORT_EMAIL", "support@example.com")


SECTIONS_EN: list[tuple[str, str]] = [
    (
        "About FlatRadar",
        "FlatRadar is an independent, unofficial monitoring tool for rental "
        "housing listings across supported platforms, including Holland2Stay, "
        "OurDomain, and Xior. We help users save filters, receive alerts when "
        "matching homes appear, and view listings on a map, calendar, and "
        "dashboard. FlatRadar is not affiliated with, endorsed by, sponsored by, "
        "or operated by any housing platform it monitors.",
    ),
    (
        "Get help",
        "If you run into a bug, have a feature request, or need help with your "
        f"account, email {CONTACT_EMAIL}. We typically reply within 1-3 business "
        "days. Please include your FlatRadar username, device model, operating "
        "system version, app platform, and the housing platform or listing URL "
        "involved so we can reproduce the issue.",
    ),
    (
        "Common questions",
        "Q. I'm not getting push notifications.\n"
        "A. On iOS or Android, open your system Settings → FlatRadar → "
        "Notifications and make sure notifications are allowed. Inside the app, "
        "check Settings → Notifications and your saved filters. Push delivery is "
        "best-effort and may be delayed by Apple Push Notification service, "
        "Firebase Cloud Messaging, or the device network state.\n\n"
        "Q. The listings shown are out of date or wrong.\n"
        "A. We fetch listing data from publicly accessible platform pages, APIs, "
        "or technical endpoints. If a source platform changes its data or "
        "availability state, FlatRadar may lag or temporarily miss details. "
        "Always double-check on the official platform website before booking or "
        "making decisions.\n\n"
        "Q. How do I reset my password?\n"
        "A. Send an email from your registered address to "
        f"{CONTACT_EMAIL} with subject \"Password reset\" and we'll help.\n\n"
        "Q. I can't sign in.\n"
        "A. Confirm your username and password. If you've forgotten your "
        f"password, email {CONTACT_EMAIL}. Some accounts can also be linked to "
        "third-party housing-platform credentials for supported workflows; those "
        "credentials are separate from your FlatRadar app password.",
    ),
    (
        "Delete your account",
        "You can delete your FlatRadar account and associated data at any time. "
        "Inside the app: Settings → Account → Delete Account. Or email "
        f"{CONTACT_EMAIL} from your registered address with subject \"Delete "
        "account\" and we will process the request within 7 days. Some data may "
        "be retained briefly for legal, security, or fraud-prevention reasons.",
    ),
    (
        "Privacy and Terms",
        "Your use of FlatRadar is governed by our Privacy Policy and Terms of "
        "Use. See the links in the footer.",
    ),
]


SECTIONS_ZH: list[tuple[str, str]] = [
    (
        "关于 FlatRadar",
        "FlatRadar 是一款独立的、非官方的多平台租房监控工具，当前覆盖 "
        "Holland2Stay、OurDomain、Xior 等支持的平台。可以保存搜索筛选、有匹配"
        "房源时发送提醒，并提供地图、日历和仪表盘视图。FlatRadar 与其监控的任何"
        "房源平台均不存在关联、授权、赞助或运营关系。",
    ),
    (
        "获取帮助",
        f"如遇到 Bug、有功能建议、或账户问题，请邮件 {CONTACT_EMAIL}。我们通常在 "
        "1-3 个工作日内回复。请在邮件里附上 FlatRadar 用户名、设备型号、系统版本、"
        "使用的平台（Web / iOS / Android），以及相关房源平台或房源链接，便于我们"
        "复现问题。",
    ),
    (
        "常见问题",
        "Q. 收不到推送通知。\n"
        "A. 在 iOS 或 Android 系统设置中打开 FlatRadar → 通知，确认已允许通知。"
        "App 内 设置 → 通知和已保存筛选条件也请确认开启。推送由 Apple Push "
        "Notification service、Firebase Cloud Messaging 或设备网络状态共同影响，"
        "属于尽力而为，偶尔可能延迟。\n\n"
        "Q. 房源信息看起来过时或错误。\n"
        "A. 房源数据来自公开可访问的平台页面、API 或技术接口。源平台数据、可订"
        "状态或页面结构变化时，FlatRadar 可能有延迟或暂时缺少部分字段。预订或做"
        "决定前请始终在对应官方平台二次确认。\n\n"
        "Q. 如何重置密码？\n"
        f"A. 从你注册时的邮箱发送邮件到 {CONTACT_EMAIL}，主题 \"密码重置\"，"
        "我们会协助处理。\n\n"
        "Q. 无法登录。\n"
        f"A. 先确认 FlatRadar 用户名和密码。如忘记密码，请邮件 {CONTACT_EMAIL}。"
        "部分账户可为支持的流程绑定第三方房源平台凭据；这些凭据与 FlatRadar App "
        "密码是分开的。",
    ),
    (
        "删除账户",
        "你可以随时删除 FlatRadar 账户及关联数据。App 内：设置 → 账户 → 删除账户。"
        f"或者从注册邮箱发邮件到 {CONTACT_EMAIL}，主题 \"删除账户\"，我们会在 7 天"
        "内处理。出于法律、安全、反欺诈等需要，少量数据可能短暂保留。",
    ),
    (
        "隐私与条款",
        "你对 FlatRadar 的使用同时受隐私政策和使用条款约束。底部有链接。",
    ),
]
