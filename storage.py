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

from typing import Callable

from mstorage import Storage  # noqa: F401


def _default_factory() -> Storage:
    from config import DB_PATH, TIMEZONE

    return Storage(DB_PATH, timezone_str=TIMEZONE)


#: 不在 Web 请求里的代码（users.py、notifier.py）打开 Storage 的方式。默认每次
#: 新开一个连接（monitor / CLI）；Web 进程启动时换成 ``app.db.storage``，请求内
#: 与路由共用同一个连接。由入口注入，这些模块就不必反过来 import Web 包——那会
#: 让 monitor 读个用户、发条通知也把 Flask 加载进来。
_factory: Callable[[], Storage] = _default_factory


def set_storage_factory(factory: Callable[[], Storage]) -> None:
    global _factory
    _factory = factory


def open_storage() -> Storage:
    """按当前进程注入的方式打开一个 Storage。调用方负责 ``close()``——Web
    请求内共用的那个实例 close 是空操作，由 teardown 统一关。"""
    return _factory()
