"""
mcore/circuit.py — 按 source 的抓取熔断
==========================================

为什么不再是 H2S 专属
--------------------
熔断、canary 恢复探测、登录抑制、专属 executor 线程——``monitor`` 里 H2S 专属
逻辑曾有 56 处引用。抽象层是对称的（``AbstractScraper`` + ``SCRAPER_REGISTRY``），
编排层却把 H2S 硬编码成了特例。这是历史遗留（H2S 曾是唯一 source）固化成的架构。

结果是**保护装在了最不需要它的那个 source 上**。保留日志全量统计「整体抓取失败」：

    xior       RateLimitError     147 次   ← 无熔断
    ourdomain  ScrapeNetworkError  67 次   ← 无熔断
    ourcampus  ScrapeNetworkError  60 次   ← 无熔断
    xior       ScrapeNetworkError  57 次   ← 无熔断
    holland2stay 全部合计            6 次   ← 有熔断

Xior 的限流按 **IP 累积**（见 ``scrapers/xior.py`` 模块头：~15–20 req/window），
整源 429 之后没有任何退避，下一轮照常再打——正是「限流最狠的时候接着撞」。

策略按 source 配，不一刀切
--------------------------
各家的失败语义不同，退避参数也就不该相同：

- H2S 的 403 要**换出口 IP** 才好 → 冷却长（30 分钟起，最长 6 小时）
- Xior 的 429 **等一会就好** → 冷却短（10 分钟起，最长 1 小时），但必须有

**429 是否熔断也按 source 配**：H2S 那边「429 等等就好、由 scrapers 内部的
RATE_LIMIT_BACKOFF 处理」的判断是实测来的，这次是推广机制，不能顺手改掉它。

网络错误一律不熔断：``monitor`` 已经有连续失败计数 + 冷却那条路，叠两层退避只会
让恢复变得难以预测。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from scrapers.base import BlockedError, RateLimitError

from .backoff import PersistedBackoff

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CircuitPolicy:
    """一个 source 的熔断参数。

    ``trips_on`` 是**异常类型白名单**——只有它列出的失败才打开熔断。刻意用白名单
    而不是黑名单：新增一种异常时默认不熔断，比默认熔断安全得多。
    """

    base_cooldown: int
    max_cooldown: int
    trips_on: tuple[type[BaseException], ...] = field(
        default_factory=lambda: (BlockedError,)
    )


#: H2S：403 要换出口 IP 才好，冷却长。**这两个数字来自既有实现，不要在这次改动
#: 里调整**——本次是把机制推广开，不是重新调它的策略。
_H2S_POLICY = CircuitPolicy(base_cooldown=1800, max_cooldown=21600)

#: Xior：429 按 IP 累积，等一会就好，所以冷却短得多，但必须有。
#: 10 分钟 ≈ 3–4 个轮次，足够让 window 内的计数掉下去。
_XIOR_POLICY = CircuitPolicy(
    base_cooldown=600, max_cooldown=3600,
    trips_on=(BlockedError, RateLimitError),
)

#: 其余 source 的默认。**新接一个平台不该顺带获得「没有熔断」这个默认值**
#: ——那正是这次要修的东西。RentCafe 系（OurDomain / OurCampus）的 403 同样
#: 要换指纹/IP 才好，沿用中等长度的冷却。
_DEFAULT_POLICY = CircuitPolicy(base_cooldown=900, max_cooldown=7200)

_POLICIES: dict[str, CircuitPolicy] = {
    "holland2stay": _H2S_POLICY,
    "xior": _XIOR_POLICY,
}


class SourceCircuits:
    """每个 source 一个熔断器，状态落库（跨进程重启存活）。"""

    def __init__(self) -> None:
        self._breakers: dict[str, PersistedBackoff] = {}
        self._storage = None

    # ── 生命周期 ────────────────────────────────────────────────────

    def load(self, storage) -> None:
        """进程启动时恢复所有已知 source 的熔断状态。"""
        self._storage = storage
        for source in self.known_sources():
            self._breaker(source).load(storage)

    @staticmethod
    def known_sources() -> tuple[str, ...]:
        """注册表里的全部 source。新增平台自动获得熔断，不需要改这里。"""
        try:
            import scrapers

            return tuple(sorted(scrapers.SCRAPER_REGISTRY))
        except Exception:  # pragma: no cover - 导入失败时退回已知策略
            return tuple(sorted(_POLICIES))

    def policy(self, source: str) -> CircuitPolicy:
        return _POLICIES.get(source, _DEFAULT_POLICY)

    def _breaker(self, source: str) -> PersistedBackoff:
        b = self._breakers.get(source)
        if b is None:
            b = PersistedBackoff(
                f"circuit_{source}", max_seconds=self.policy(source).max_cooldown,
            )
            if self._storage is not None:
                b.load(self._storage)
            self._breakers[source] = b
        return b

    # ── 读 ──────────────────────────────────────────────────────────

    def remaining(self, source: str) -> int:
        return self._breaker(source).remaining()

    def reason(self, source: str) -> str:
        return self._breaker(source).reason

    def fail_streak(self, source: str) -> int:
        return self._breaker(source).fail_streak

    def plan(self, source: str, *, n_tasks: int) -> tuple[str, int]:
        """本轮这个 source 该跑几个 task。

        Returns
        -------
        ``(mode, n)``
          none    没有任务
          open    熔断中，一个都不跑
          canary  冷却到期，只放 1 个探测恢复（成功了下一轮才全量放开）
          normal  正常
        """
        if n_tasks <= 0:
            return "none", 0
        b = self._breaker(source)
        if b.remaining() > 0:
            return "open", 0
        if b.fail_streak > 0:
            return "canary", 1
        return "normal", n_tasks

    # ── 写 ──────────────────────────────────────────────────────────

    def trips_on(self, source: str, exc: BaseException) -> bool:
        return isinstance(exc, self.policy(source).trips_on)

    def trip(self, source: str, exc: BaseException, *, storage=None) -> int:
        """按策略打开熔断；该异常不该熔断时返回 0（且不改任何状态）。"""
        policy = self.policy(source)
        if not isinstance(exc, policy.trips_on):
            return 0
        b = self._breaker(source)
        streak = b.bump()
        cooldown = min(
            policy.max_cooldown,
            policy.base_cooldown * (2 ** max(0, streak - 1)),
        )
        b.open(cooldown, reason=f"{type(exc).__name__}: {exc}",
               storage=storage or self._storage)
        logger.error(
            "🚫 source %s 熔断：连续第 %d 次（%s），暂停 %d 秒；其它 source 继续运行。原因: %s",
            source, streak, type(exc).__name__, cooldown, exc,
        )
        return cooldown

    def expire(self, source: str, *, storage=None) -> None:
        """冷却到点：允许 canary，但**保留连败计数**（下次失败继续往上爬）。"""
        self._breaker(source).expire(storage or self._storage)

    def recover(self, source: str, *, storage=None) -> None:
        """canary 成功：关闭熔断并清零连败。"""
        b = self._breaker(source)  # noqa: F841 - 下面要用
        if b.fail_streak or b.remaining():
            logger.info(
                "✅ source %s canary 成功，关闭熔断（之前连续失败 %d 次）",
                source, b.fail_streak,
            )
        b.reset(storage or self._storage)
