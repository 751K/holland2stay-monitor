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
``factor=2`` / ``max_multiplier=2``。乘的是**当前时段那一档**基准
（见 config 的 SOURCE_PEAK_MIN_INTERVALS），于是：

    峰内   360 → 720 秒     720 在实测干净的 381 秒之上，留足余量
    峰外   180 → 360 秒     360 仍低于峰外自然节奏 381 秒，闸门保持不生效

上限取 2 而不是 4，正是为了后一行：峰外每轮 429 概率只有 0.09%，本来就不需要
退避；若允许 ×4，一次**峰内**的 429 会把峰外一起压到 720 秒，在没有风险的时段
白白减半抓取频率。720 秒之上没有任何数据支持，真不够用时该拿着证据再放开。

``calm_seconds=14400``（4 小时）：距上次被限流满 4 小时才降一档。

**按时间而不是按轮数**——原先是「连续 8 轮干净」，实测直接翻车：

    08-21 08:52   ×2 → ×1   攒够 8 轮
    08-21 09:14   ×1 → ×2   22 分钟后就撞回去

根子在于同一个轮数在不同时段意味着完全不同的耐心：峰内 20 秒一轮，8 轮只有
2 分钟；峰外 300 秒一轮，8 轮是 40 分钟。而限流恢复只跟真实时间有关，跟我们
跑了几轮无关。这和 mcore/health.py 里把「完整扫描率」换成「距上次完整扫描
多久」是同一类修正。
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

_MULT_KEY = "pacing_mult_{}"
#: 当前这段「干净期」的起点（墙钟 epoch 秒）。
#:
#: 键名和旧的 ``pacing_calm_{}`` **刻意不同**：那个存的是轮数计数，语义已经变了。
#: 沿用同一个键的话，升级后第一次读会把一个小整数（比如 7）当成 1970 年的时间戳，
#: 算出「已经干净了 56 年」，于是立刻降一档——正是这次要消灭的行为。
#: 旧键留在库里不管，它不再被任何代码读取。
_CALM_SINCE_KEY = "pacing_calm_since_{}"


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
        max_multiplier: float = 2.0,
        calm_seconds: float = 14400.0,
    ) -> None:
        self._factor = max(1.0, float(factor))
        self._max = max(1.0, float(max_multiplier))
        self._calm_seconds = max(1.0, float(calm_seconds))
        self._mult: dict[str, float] = {}
        #: source → 当前干净期的起点（墙钟）。倍率为 1 时无意义。
        self._calm_since: dict[str, float] = {}
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
                since = self._read_float(storage, _CALM_SINCE_KEY.format(src), 0.0)
                if since > 0:
                    self._calm_since[src] = since
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
            storage.set_meta(_CALM_SINCE_KEY.format(source),
                             str(self._calm_since.get(source, 0.0)))
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

    def penalize(self, source: str, *, storage=None, now: float | None = None) -> bool:
        """被限流了：间隔翻倍，干净期从此刻重新开始计时。

        返回倍率是否真的变了（已到上限则不变）。**即使没变也要重置计时**——
        封顶不等于风险消失，反而说明还在挨打，不该让它继续朝降档爬。

        只该由**确实是 429** 的失败调用。403、网络错误、平台维护都不是「打得
        太勤」造成的，拉长间隔既治不了它们，又会白白拖慢恢复。
        """
        t = time.time() if now is None else now
        old = self._mult.get(source, 1.0)
        new = min(self._max, old * self._factor)
        self._calm_since[source] = t
        self._mult[source] = new
        self._save(source, storage)
        if new == old:
            return False
        logger.warning(
            "source %s 被限流，最小抓取间隔 ×%.3g → ×%.3g（上限 ×%.3g）"
            "——抓得越勤实际拿到数据反而越晚",
            source, old, new, self._max,
        )
        return True

    def relax(self, source: str, *, storage=None, now: float | None = None) -> bool:
        """干净地抓完一轮。距上次被限流满 ``calm_seconds`` 才降一档。

        返回本次是否真的降了档。

        用墙钟而不是单调钟，理由和 mcore/backoff.py 一样：状态要跨进程重启
        存活，而 ``monotonic()`` 的零点是进程启动，持久化它毫无意义。
        """
        t = time.time() if now is None else now
        if self._mult.get(source, 1.0) <= 1.0:
            # 已经在基准节奏上：清掉计时起点，不必反复写库
            if self._calm_since.pop(source, None) is not None:
                self._save(source, storage)
            return False

        since = self._calm_since.get(source)
        if since is None:
            # 倍率是从库里恢复的、但计时起点丢了（旧版本升上来，或 meta 写坏）。
            # 从现在开始计时，而不是当作「已经干净很久」立刻降档。
            self._calm_since[source] = t
            self._save(source, storage)
            return False

        elapsed = t - since
        if elapsed < 0:
            # 时钟回拨。重新起算而不是当场降档——保守一侧是**保持退避**。
            logger.warning(
                "source %s 的节奏计时起点在未来（时钟回拨？），重新起算", source,
            )
            self._calm_since[source] = t
            self._save(source, storage)
            return False

        if elapsed < self._calm_seconds:
            return False

        old = self._mult.get(source, 1.0)
        new = max(1.0, old / self._factor)
        self._mult[source] = new
        if new <= 1.0:
            self._calm_since.pop(source, None)
        else:
            self._calm_since[source] = t
        self._save(source, storage)
        logger.info(
            "source %s 已 %.1f 小时没被限流，最小抓取间隔 ×%.3g → ×%.3g",
            source, elapsed / 3600.0, old, new,
        )
        return True

    def reset(self, source: str, *, storage=None) -> None:
        """回到基准节奏。给运维和测试用，正常流程不调。"""
        self._mult[source] = 1.0
        self._calm_since.pop(source, None)
        self._save(source, storage)
