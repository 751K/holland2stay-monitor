"""
notifier_channels — 独立渠道实现的轻量子包
==========================================

目前只放 APNs（与现有 notifier.py 内的 iMessage/Telegram/Email/WhatsApp
四个 BaseNotifier 子类并列；将来若把那 4 个也从 notifier.py 拆出来，
本包就是统一目录）。

为什么不直接进 notifier.py？
----------------------------
notifier.py 是 monitor.py 长期依赖的稳定模块；新加渠道的目标是
"完全不动" 现有推送方式（用户硬约束）。把 APNs 隔离到独立子包，
保证 import 顺序、依赖加载、错误处理都互不影响。
"""
