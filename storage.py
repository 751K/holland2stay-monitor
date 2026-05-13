"""
storage.py — SQLite 持久化层
==============================
实现已拆分至 `mstorage/` 包，本文件仅保留向后兼容的 re-export。

模块结构
--------
mstorage/
  _base.py          StorageBase  — 连接 / schema / meta / 生命周期
  _listings.py      ListingOps  — diff / mark_notified / 面板查询
  _charts.py        ChartOps    — 10 个统计图表
  _notifications.py NotificationOps — web_notifications CRUD
  _map_calendar.py  MapCalendarOps — 地图坐标缓存 + 日历查询
  _retry.py         RetryQueueOps — 竞败重试队列持久化
"""

from mstorage import Storage  # noqa: F401
