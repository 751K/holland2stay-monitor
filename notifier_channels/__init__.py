"""
notifier_channels — 独立渠道实现的轻量子包
==========================================

目前包含 APNs（iOS）和 FCM（Android）两个推送渠道。
与现有 notifier.py 内的 iMessage/Telegram/Email/WhatsApp
四个 BaseNotifier 子类并列。

为什么不直接进 notifier.py？
----------------------------
notifier.py 是 monitor.py 长期依赖的稳定模块；新加渠道的目标是
"完全不动" 现有推送方式（用户硬约束）。把 APNs / FCM 隔离到独立子包，
保证 import 顺序、依赖加载、错误处理都互不影响。
"""
