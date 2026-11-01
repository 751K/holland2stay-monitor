"""进程内滑动窗口限流。

Web 侧所有内存限流共用这一份实现与清理逻辑，取代此前各路由/模块各写一份
dict 的做法。当前部署是单进程多线程（gunicorn ``--workers=1``），按进程计数
是准确的；如果以后改成多 worker，必须换成共享存储（Redis/SQLite），否则每个
worker 各有一套桶，实际限额会被放大。

公网端点会见到大量一次性 IP。每个窗口在 ``SWEEP_INTERVAL_SEC`` 到期后的第一次
访问时清理空闲键，避免 ``{ip: [...]}`` 随访问来源无限增长。
"""

from __future__ import annotations

import time
from collections import deque

#: 清理空闲键的间隔（秒）。
SWEEP_INTERVAL_SEC = 300.0


class SlidingWindow:
    """按 key 记录时间戳的滑动窗口。

    只保存命中时间，不做计数缓存；窗口通常很小，线性扫描足够。
    ``clock`` 可注入，便于测试推进时间。
    """

    def __init__(
        self,
        name: str,
        window: float,
        *,
        clock=time.monotonic,
    ) -> None:
        self.name = name
        self.window = float(window)
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._last_sweep = clock()

    # ── 内部 ────────────────────────────────────────────────────────

    def _prune(self, key: str, now: float) -> deque[float] | None:
        bucket = self._hits.get(key)
        if not bucket:
            self._hits.pop(key, None)
            return None
        cutoff = now - self.window
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if not bucket:
            self._hits.pop(key, None)
            return None
        return bucket

    def _sweep(self, now: float) -> None:
        if now - self._last_sweep < SWEEP_INTERVAL_SEC:
            return
        self._last_sweep = now
        for key in list(self._hits):
            self._prune(key, now)

    # ── 对外 ────────────────────────────────────────────────────────

    def hits(self, key: str, *, now: float | None = None) -> list[float]:
        """返回窗口内的命中时间戳（已剔除过期项）。"""
        now = self._clock() if now is None else now
        self._sweep(now)
        bucket = self._prune(key, now)
        return list(bucket) if bucket else []

    def would_allow(self, key: str, limit: int, *, now: float | None = None) -> bool:
        """只检查，不记录。"""
        return len(self.hits(key, now=now)) < limit

    def record(self, key: str, *, now: float | None = None) -> None:
        """记录一次命中。"""
        now = self._clock() if now is None else now
        self._sweep(now)
        self._prune(key, now)
        self._hits.setdefault(key, deque()).append(now)

    def allow(self, key: str, limit: int, *, now: float | None = None) -> bool:
        """检查并记录：未超限则记录并返回 True。"""
        now = self._clock() if now is None else now
        self._sweep(now)
        bucket = self._prune(key, now)
        if bucket is not None and len(bucket) >= limit:
            return False
        self._hits.setdefault(key, deque()).append(now)
        return True

    def clear(self, key: str) -> None:
        self._hits.pop(key, None)

    def clear_all(self) -> None:
        self._hits.clear()
