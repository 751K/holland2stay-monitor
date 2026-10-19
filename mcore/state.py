"""进程级单例的共享状态。

目前只包含自动预订重试队列。其余单例（prewarm 缓存、熔断器、退避、节流）
仍留在 monitor.py，等 RuntimeState 那一步一起搬——搬它们需要同时处理
``_bind_persistent_state`` 与热重载，单独拆没有收益。
"""
from __future__ import annotations

from mcore.booking import RetryQueue

retry_queue = RetryQueue()
