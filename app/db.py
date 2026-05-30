"""
SQLite Storage 工厂
====================

Flask 请求上下文中复用同一 SQLite 连接（g._storage），teardown_appcontext
自动关闭；非请求上下文（monitor / CLI / 测试）仍由调用方负责 close()。

依赖
----
- config.DB_PATH, config.TIMEZONE
- storage.Storage
- web.py 注册的 teardown_appcontext 处理器
"""
from __future__ import annotations

from flask import g, has_request_context

from config import DB_PATH, TIMEZONE
from storage import Storage


def storage() -> Storage:
    """返回一个 Storage 实例。

    - Flask 请求上下文中：首次调用创建连接并存入 g._storage，后续调用复用
      同一实例。连接的真正关闭由 web.py 的 teardown_appcontext 完成。
    - 非请求上下文（monitor / CLI / 测试）：每次调用创建新实例，调用方需
      自行 close()。
    """
    if has_request_context():
        st = getattr(g, '_storage', None)
        if st is None:
            st = Storage(DB_PATH, timezone_str=TIMEZONE)
            st._teardown_managed = True
            g._storage = st
        return st
    return Storage(DB_PATH, timezone_str=TIMEZONE)
