"""
SQLite Storage 工厂
====================

每个请求拿一个新的 Storage 实例。当前模型：
- 每路由开 → 用 → 关，不复用连接
- Storage 内部启用 WAL，读写不互相阻塞
- 后续 Stage 6 若要做请求作用域池化，统一改这一处

依赖
----
- config.DB_PATH, config.TIMEZONE
- storage.Storage
"""
from __future__ import annotations

from config import DB_PATH, TIMEZONE
from storage import Storage


def storage() -> Storage:
    """返回一个新的 Storage 实例。调用方负责 close()。"""
    return Storage(DB_PATH, timezone_str=TIMEZONE)
