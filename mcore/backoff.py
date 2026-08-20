"""
mcore/backoff.py — 跨进程重启存活的退避 / 熔断 / 节流状态
============================================================

为什么需要它
------------
这些状态原本全是 ``monitor`` 的模块级全局，重启即清零：

    _h2s_circuit_open_until / _fail_streak    H2S 熔断（最长 6 小时退避）
    _h2s_login_blocked_until                  登录抑制（1 小时）
    blocked_fail_streak                       403 指数退避（最长 2 小时）
    _last_*_notify_at ×6                      全部告警节流

也就是说：**正在被 CF 封、退避已经拉到 2 小时的时候，部署一次，立刻满速重打。**
2026-08-20 一天之内部署了 12 次 = 12 次退避清零。

而这个判断项目自己写下过 —— ``monitor._apply_source_intervals`` 里：

    时间戳存 meta，重启后仍然生效——否则频繁重启会绕过节流，**正是限流最狠的
    时候**（重启往往就是因为出问题了）。

同一个判断、同一个文件，当时只落地在了「source 抓取间隔」上。本模块把它推广到
熔断、退避与告警节流。

墙钟 vs 单调钟
--------------
原实现用 ``time.monotonic()``。它的零点是**进程启动**，所以持久化它毫无意义：
存进去 5000，重启后读出来还是 5000，而 ``now`` 变成了 3。

要跨重启就只能用 ``time.time()``（墙钟），代价是要处理 NTP 跳变。处理方式是
**把剩余时间钳到配置上限**：永远不会等得比配置的最长退避还久。钳的方向是安全的
——时钟往回跳一次能让 H2S 停摆到天荒地老，而且没有任何日志说得清为什么。

存储
----
落在 ``meta`` 表的两个 key：``backoff:<name>:until`` / ``backoff:<name>:streak``。
读只在 ``load()``（进程启动一次），写只在状态真正变化时——不是每轮。
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


class PersistedBackoff:
    """一段「截止时刻 + 连败计数」，落库后跨进程重启存活。

    Parameters
    ----------
    name
        meta key 的前缀，同一进程内必须唯一。
    max_seconds
        配置允许的最长退避。同时是**时钟跳变的钳位上限**，见模块文档。
    """

    __slots__ = ("_name", "_max", "_until", "_streak", "reason")

    def __init__(self, name: str, *, max_seconds: float) -> None:
        self._name = name
        self._max = float(max_seconds)
        self._until = 0.0
        self._streak = 0
        self.reason = ""

    # ── 持久化 ──────────────────────────────────────────────────────

    @property
    def _key_until(self) -> str:
        return f"backoff:{self._name}:until"

    @property
    def _key_streak(self) -> str:
        return f"backoff:{self._name}:streak"

    @property
    def _key_reason(self) -> str:
        return f"backoff:{self._name}:reason"

    def load(self, storage) -> None:
        """进程启动时读一次。meta 损坏时当作「没有退避」，绝不抛。"""
        self._until = _as_float(storage.get_meta(self._key_until, default=""))
        self._streak = int(_as_float(storage.get_meta(self._key_streak, default="")))
        self.reason = storage.get_meta(self._key_reason, default="") or ""
        if self.remaining() > 0:
            logger.info(
                "恢复退避状态 %s：剩余 %d 秒，连败 %d 次%s",
                self._name, self.remaining(), self._streak,
                f"（原因: {self.reason}）" if self.reason else "",
            )

    def _persist(self, storage) -> None:
        try:
            storage.set_meta(self._key_until, str(self._until))
            storage.set_meta(self._key_streak, str(self._streak))
            storage.set_meta(self._key_reason, self.reason or "")
        except Exception:
            # 落库失败最多让这段退避退化回「进程内」，不该拖垮调用方
            logger.warning("退避状态 %s 落库失败（重启后会丢）", self._name, exc_info=True)

    # ── 读 ──────────────────────────────────────────────────────────

    def remaining(self) -> int:
        """还要等几秒。已过期或未设置返回 0。

        **钳到 ``max_seconds``**：墙钟往回跳时库里的 deadline 会看起来远在未来，
        不钳的话一次跳变就能把退避放大到不可理喻的长度。
        """
        if self._until <= 0:
            return 0
        left = self._until - time.time()
        if left <= 0:
            return 0
        return int(min(left, self._max))

    @property
    def fail_streak(self) -> int:
        return self._streak

    # ── 写 ──────────────────────────────────────────────────────────

    def bump(self) -> int:
        """连败 +1（还没落库；随后的 ``open()`` 会一起写）。"""
        self._streak += 1
        return self._streak

    def open(self, seconds: float, *, reason: str = "", storage=None):
        """打开退避窗口到 ``now + seconds``，并落库。

        取 ``max(现有, 新的)``：多条路径可能同时想延长同一段退避，谁都不该把
        别人已经设好的更长窗口缩短。
        """
        self._until = max(self._until, time.time() + float(seconds))
        if reason:
            self.reason = reason
        if storage is not None:
            self._persist(storage)
        return self

    def reset(self, storage=None) -> None:
        """关闭退避、清零连败，并落库。"""
        self._until = 0.0
        self._streak = 0
        self.reason = ""
        if storage is not None:
            self._persist(storage)

    def expire(self, storage=None) -> None:
        """立刻让窗口过期，但**保留连败计数**。

        和 ``reset()`` 的区别正是连败计数：``reset`` 表示「问题解决了」（canary
        成功），``expire`` 表示「这一轮的等待到点了」。测试里的时间旅行也用它，
        免得到处去戳 ``_until`` 这个私有字段。
        """
        self._until = 0.0
        if storage is not None:
            self._persist(storage)

    def claim(self, window_seconds: float, *, storage=None) -> bool:
        """告警节流：窗口内第一次调用返回 True 并开启新窗口，其余返回 False。

        和 ``open()`` 的区别是它**先判后写**，用来表达「这条告警现在该不该发」。
        """
        if self.remaining() > 0:
            return False
        self._until = time.time() + float(window_seconds)
        if storage is not None:
            self._persist(storage)
        return True


def _as_float(raw: str) -> float:
    """meta 里存的是字符串，损坏时当 0——绝不让一条脏数据把 monitor 拦在门外。"""
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0
