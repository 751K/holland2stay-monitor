"""按 429 历史自动伸缩 source 的最小抓取间隔。

与熔断的分工
------------
两者都是退避，但管的不是同一件事，缺一不可：

    mcore/circuit.py    出事**之后**停多久      （600s 起，最长 1 小时）
    本模块              平时**多久打一次**      （180s 起，最长 720s）

只有熔断的话，冷却结束、canary 通过之后节奏立刻回到原值，于是过三五十分钟又
撞一次——这正是生产实测的形态：2026-08-10 起每天约 10 次 429 爆发，从没停过。
熔断能从每次碰撞里恢复，但它不改变**导致碰撞的那个节奏**。

为什么必须自动
--------------
`_DEFAULT_SOURCE_MIN_INTERVALS` 是手工自上而下试出来的，config.py 里那段注释
写着「600 → 300 都干净，现在试 180……若 180 也干净，下一档再往 120 试」。

而 180 并不干净：

    08-18  72 次   08-19  80 次   08-20  39 次    （按 task 计的 429）

这套调法默认「不干净的话会有人注意到并退回上一档」。没有人退。把这一步交给
代码之后，就不再依赖那个假设。

参数取值
--------
``factor=2`` / ``max_multiplier=4``  →  180 → 360 → 720 秒。360 与 720 恰好
夹住上面注释里实测「干净」的 300 与 600，两次碰撞即可收敛到安全区。

不设更大的上限，是因为**抓得慢的代价是真实的**：房源出现到被发现最多晚一个
间隔。但对照现状——每天 10 次 429，每次触发 600 秒熔断，Xior 事实上每天已经有
约 100 分钟完全抓不到——720 秒的稳态远好过 180 秒加上反复黑洞。

``calm_rounds=8``：连续 8 轮干净才降一档。在 720 秒档上约 96 分钟，在 360 秒
档上约 48 分钟。降得太快会在阈值附近来回振荡，每次振荡都要付一次熔断。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_MULT_KEY = "pacing_mult_{}"
_CALM_KEY = "pacing_calm_{}"


class AdaptivePacing:
    """每个 source 一个倍率，落库（跨进程重启存活）。

    重启存活的理由和 mcore/backoff.py 一样：supervisor 的 autorestart 恰恰会在
    故障期频繁重启，倍率若只活在进程内，一次部署就把好不容易退到的安全节奏清
    回 180 秒——而部署往往就发生在出问题的时候。
    """

    def __init__(
        self,
        *,
        factor: float = 2.0,
        max_multiplier: float = 4.0,
        calm_rounds: int = 8,
    ) -> None:
        self._factor = max(1.0, float(factor))
        self._max = max(1.0, float(max_multiplier))
        self._calm_rounds = max(1, int(calm_rounds))
        self._mult: dict[str, float] = {}
        self._calm: dict[str, int] = {}
        self._storage = None

    # ── 状态 ────────────────────────────────────────────────────

    def load(self, storage) -> None:
        """从库里恢复。**绝不抛**——节奏是优化，不是抓取的前提。

        读不到最多退化成「从 1 倍重新学」，不该把 monitor 拦在启动阶段。
        """
        self._storage = storage
        try:
            for src in self._known(storage):
                self._mult[src] = self._read_float(storage, _MULT_KEY.format(src), 1.0)
                self._calm[src] = int(self._read_float(storage, _CALM_KEY.format(src), 0.0))
        except Exception:
            logger.warning("恢复抓取节奏失败（按 1 倍处理）", exc_info=True)

    @staticmethod
    def _known(storage) -> tuple[str, ...]:
        """要恢复哪些 source，取自**注册表**而不是遥测表。

        用遥测表（round_stats_sources）会漏掉「meta 里有倍率、但保留期内还没
        写过遥测行」的 source——那种 source 恰好是刚被限流拖住的那个。注册表
        是声明式的，新增平台自动被覆盖。
        """
        try:
            from mcore.circuit import SourceCircuits

            return SourceCircuits.known_sources()
        except Exception:
            return ()

    @staticmethod
    def _read_float(storage, key: str, default: float) -> float:
        try:
            raw = storage.get_meta(key, default="")
        except Exception:
            return default
        try:
            v = float(raw or default)
        except (TypeError, ValueError):
            return default
        return v if v > 0 else default

    def _save(self, source: str, storage) -> None:
        if storage is None:
            return
        try:
            storage.set_meta(_MULT_KEY.format(source), str(self._mult.get(source, 1.0)))
            storage.set_meta(_CALM_KEY.format(source), str(self._calm.get(source, 0)))
        except Exception:
            # 写不进去就只在进程内生效，比整个停掉强
            logger.debug("写 %s 抓取节奏失败（已忽略）", source, exc_info=True)

    # ── 查询 ────────────────────────────────────────────────────

    def multiplier(self, source: str) -> float:
        return self._mult.get(source, 1.0)

    def gap_for(self, source: str, base_gap: int) -> int:
        """本 source 当前应当遵守的最小间隔（秒）。

        ``base_gap <= 0`` 表示用户显式关掉了该 source 的节流。倍率此时一律不
        生效——**关掉就是关掉**，不该被自适应偷偷打开。
        """
        if base_gap <= 0:
            return 0
        return int(round(base_gap * self.multiplier(source)))

    # ── 反馈 ────────────────────────────────────────────────────

    def penalize(self, source: str, *, storage=None) -> bool:
        """被限流了：间隔翻倍。返回倍率是否真的变了（已到上限则不变）。

        只该由**确实是 429** 的失败调用。403、网络错误、平台维护都不是「打得
        太勤」造成的，拉长间隔既治不了它们，又会白白拖慢恢复。
        """
        old = self._mult.get(source, 1.0)
        new = min(self._max, old * self._factor)
        self._calm[source] = 0
        if new == old:
            self._save(source, storage)
            return False
        self._mult[source] = new
        self._save(source, storage)
        logger.warning(
            "source %s 被限流，最小抓取间隔 ×%.3g → ×%.3g（上限 ×%.3g）"
            "——抓得越勤实际拿到数据反而越晚",
            source, old, new, self._max,
        )
        return True

    def relax(self, source: str, *, storage=None) -> bool:
        """干净地抓完一轮。攒够 ``calm_rounds`` 轮才降一档。

        返回本次是否真的降了档。
        """
        if self._mult.get(source, 1.0) <= 1.0:
            # 已经在基准节奏上，不必攒计数，也不必反复写库
            if self._calm.get(source):
                self._calm[source] = 0
                self._save(source, storage)
            return False

        self._calm[source] = self._calm.get(source, 0) + 1
        if self._calm[source] < self._calm_rounds:
            self._save(source, storage)
            return False

        old = self._mult.get(source, 1.0)
        new = max(1.0, old / self._factor)
        self._mult[source] = new
        self._calm[source] = 0
        self._save(source, storage)
        logger.info(
            "source %s 连续 %d 轮没被限流，最小抓取间隔 ×%.3g → ×%.3g",
            source, self._calm_rounds, old, new,
        )
        return True

    def reset(self, source: str, *, storage=None) -> None:
        """回到基准节奏。给运维和测试用，正常流程不调。"""
        self._mult[source] = 1.0
        self._calm[source] = 0
        self._save(source, storage)
