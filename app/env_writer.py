"""
.env 写入：线程锁包装层
========================

为什么需要这一层
----------------
config.write_env_key() 是无锁的原始写入实现（read → 内存修改 → 原地写回）。
Web 多线程并发场景下（如 settings POST + secret_key 自动生成同时发生），
两个线程的"读 → 写"序列可能互相覆盖。

本模块用一把进程内 Lock 串行化所有写入。crypto.py 的写入是单点触发
（首次启动时生成密钥），不会与 web 并发，因此可继续直接调 config.write_env_key
而不强制走本模块。

依赖
----
- threading（标准库）
- config.write_env_key（实际写入实现）
"""
from __future__ import annotations

import threading

from config import write_env_key as _raw_write_env_key

_lock = threading.Lock()


def write_env_key(key: str, value: str) -> None:
    """
    写入或更新 .env 文件中的单个键值对（带进程内串行化锁）。

    线程锁确保并发写入不会互相覆盖。实际 read-modify-write 由
    config.write_env_key() 完成（绕开 Docker bind-mount 的 atomic rename 限制）。
    """
    with _lock:
        _raw_write_env_key(key, value)
