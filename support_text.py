"""
FlatRadar 公开支持页面文案。

⚠️ App Store Connect 提交时**必填的 Support URL** 指向 `/support`。
苹果审核员会点开验证页面真实存在、有人维护、能联系到开发者。

字段
----
- `SECTIONS_EN` / `SECTIONS_ZH`: 段落列表，每段一个 (title, body) 元组
- `CONTACT_EMAIL`: 用户/审核员唯一联系邮箱

改动建议
--------
新增 FAQ 时 append 到 SECTIONS_*；如果 iOS 端有同款 in-app help 页，
也要同步那边的文案，避免出现 "App 内说 X，网页说 Y" 的不一致。
"""
from __future__ import annotations


CONTACT_EMAIL = "k1012922528@gmail.com"


SECTIONS_EN: list[tuple[str, str]] = [
    (
        "About FlatRadar",
        "FlatRadar is an independent, unofficial monitoring tool for Holland2Stay "
        "housing listings. We help users save filters, receive push alerts when "
        "matching homes appear, and view listings on a map / calendar. FlatRadar "
        "is not affiliated with, endorsed by, or operated by Holland2Stay.",
    ),
    (
        "Get help",
        "If you run into a bug, have a feature request, or need help with your "
        f"account, email {CONTACT_EMAIL}. We typically reply within 1-3 business "
        "days. Please include your FlatRadar username, your device model, and "
        "iOS version so we can reproduce the issue.",
    ),
    (
        "Common questions",
        "Q. I'm not getting push notifications.\n"
        "A. Open iOS Settings → FlatRadar → Notifications and make sure they are "
        "allowed. Inside the app, check Settings → Notifications. Push delivery "
        "is best-effort and may be delayed by Apple Push Notification service.\n\n"
        "Q. The listings shown are out of date or wrong.\n"
        "A. We fetch listing data from publicly accessible Holland2Stay endpoints. "
        "If the source data changes, FlatRadar may lag a few minutes. Always "
        "double-check on the official Holland2Stay website before booking.\n\n"
        "Q. How do I reset my password?\n"
        "A. Send an email from your registered address to "
        f"{CONTACT_EMAIL} with subject \"Password reset\" and we'll help.\n\n"
        "Q. I can't sign in.\n"
        "A. Confirm your username and password. If you've forgotten your "
        f"password, email {CONTACT_EMAIL}. Note that signing in with your "
        "Holland2Stay account requires that you have explicitly enabled the "
        "\"Allow H2S login\" option for your account.",
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
        "FlatRadar 是一款独立的、非官方的 Holland2Stay 房源监控工具。可以保存搜索"
        "筛选、有匹配房源时推送通知、并提供地图 / 日历视图。FlatRadar 与 Holland2Stay "
        "不存在关联、授权或运营关系。",
    ),
    (
        "获取帮助",
        f"如遇到 Bug、有功能建议、或账户问题，请邮件 {CONTACT_EMAIL}。我们通常在 "
        "1-3 个工作日内回复。请在邮件里附上 FlatRadar 用户名、设备型号、iOS 版本，"
        "便于我们复现问题。",
    ),
    (
        "常见问题",
        "Q. 收不到推送通知。\n"
        "A. 打开 iOS 设置 → FlatRadar → 通知，确认已允许通知。App 内 设置 → 通知 "
        "也确认开关已开。推送由 Apple Push Notification 服务投递，属于尽力而为，"
        "偶尔可能延迟。\n\n"
        "Q. 房源信息看起来过时或错误。\n"
        "A. 房源数据从公开可访问的 Holland2Stay 接口拉取。源数据变动时 FlatRadar "
        "可能有几分钟延迟。预订前请始终在 Holland2Stay 官网二次确认。\n\n"
        "Q. 如何重置密码？\n"
        f"A. 从你注册时的邮箱发送邮件到 {CONTACT_EMAIL}，主题 \"密码重置\"，"
        "我们会协助处理。\n\n"
        "Q. 无法登录。\n"
        f"A. 先确认用户名和密码。如忘记密码，请邮件 {CONTACT_EMAIL}。注意：用 "
        "Holland2Stay 账号登录需要管理员/用户显式启用 \"允许 H2S 登录\" 选项。",
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
