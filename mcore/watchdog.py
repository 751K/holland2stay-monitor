"""mcore/watchdog.py — 数据退化告警
=====================================

现有的 admin 告警全部**对异常触发**：代理失效、H2S 熔断、未分类异常、管线异常。
它们看不见的是**没有异常的故障**——某个 source 一直「成功」返回 0 条，或者完整
扫描率悄悄从 100% 滑到 40%。2026-06-13 起那次 7 周静默停摆就是这一类的极端形态：
进程没崩，只是不干活了。

本模块拿 ``mcore.health`` 的指标跑规则，产出告警；发送由 monitor 负责。

两个刻意的设计
--------------
**节流状态写进 meta，不放内存。** 现有的 ``_should_notify_internal()`` 是模块级
变量，进程一重启就清零，而 supervisor 的 autorestart 恰恰会在故障时频繁重启
——最该节流的时候节流失效。按规则 key 存 meta，重启后仍然生效。

**恢复也要通知。** 只报警不报恢复，等于逼人继续 ssh 上去确认好了没有，那就没
解决最初的问题。上一轮的活跃规则集存在 meta 里，消失的 key 即产出 recovered。
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

from mcore.health import (
    STATUS_DOWN,
    STATUS_WARN,
    fmt_ts,
    silent_round_streak,
    source_health,
)

logger = logging.getLogger(__name__)

_ACTIVE_META_KEY = "watchdog_active"
_FIRED_META_PREFIX = "watchdog_fired:"

# 同一条告警的最小重发间隔。默认 6 小时——退化类故障不像异常那样自己会好，
# 报得太勤只会让人开始忽略它。
_REPEAT_INTERVAL = float(os.environ.get("WATCHDOG_REPEAT_INTERVAL", "21600"))

# 连续多少轮全局零房源才报。比单 source 的阈值高：整轮零可能是所有 source
# 恰好同时空窗，得给它更长的确认期。
_SILENT_ROUNDS = int(os.environ.get("WATCHDOG_SILENT_ROUNDS", "6"))

LEVEL_WARN = "warn"
LEVEL_DOWN = "down"
LEVEL_RECOVERED = "recovered"


@dataclass(slots=True)
class Alert:
    key: str      # 节流键，也是「同一条告警」的身份，如 "source_down:xior"
    level: str
    title: str
    body: str

    def message(self) -> str:
        return f"{self.title}\n\n{self.body}" if self.body else self.title


# ── 规则 ────────────────────────────────────────────────────────────


def evaluate(storage, *, window: int | None = None) -> list[Alert]:
    """跑一遍规则，返回**当前处于告警态**的条目（不含恢复，恢复由 diff 得出）。"""
    kwargs = {"window": window} if window else {}
    alerts: list[Alert] = []

    for h in source_health(storage, **kwargs):
        if h.status == STATUS_DOWN:
            alerts.append(Alert(
                key=f"source_down:{h.source}",
                level=LEVEL_DOWN,
                title=f"⛔ {h.source} 连续抓取失败",
                body=(
                    f"连续失败 {h.fail_streak} 轮"
                    + (f"｜错误 {h.last_error}" if h.last_error else "")
                    + f"｜最近成功 {fmt_ts(h.last_success_at, fallback='无')}"
                ),
            ))
            # down 期间不再叠加该 source 的 warn——它们说的是同一件事。
            continue

        if h.status != STATUS_WARN:
            continue

        # warn 可能由多条原因触发，拆成独立告警：完整率下滑和零房源是两种
        # 不同的故障，合成一条会让恢复判定跟着糊掉。
        if h.zero_streak and h.max_listings > 0:
            alerts.append(Alert(
                key=f"source_zero:{h.source}",
                level=LEVEL_WARN,
                title=f"⚠️ {h.source} 抓取成功但零房源",
                body=(
                    f"连续 {h.zero_streak} 轮抓到 0 条"
                    f"｜窗口内最高 {h.max_listings} 条"
                    f"｜最近非零 {fmt_ts(h.last_nonzero_at, fallback='无')}"
                ),
            ))
        # 判据是「多久没有完整扫描」而不是完整率——分层抓取让后者对 H2S
        # 永久为真，见 mcore/health.py 的 STALE_FULL_SCAN_SECONDS。
        if any("没有完整扫描" in r for r in h.reasons):
            alerts.append(Alert(
                key=f"stale_full_scan:{h.source}",
                level=LEVEL_WARN,
                title=f"⚠️ {h.source} 迟迟没有完整扫描",
                body=(
                    f"已 {h.stale_full_scan_seconds / 60:.0f} 分钟没有完整轮"
                    f"｜最近一次 {fmt_ts(h.last_complete_at, fallback='无记录')}"
                    "｜期间 stale 收敛不会执行，下架判定会滞后"
                ),
            ))

    streak = silent_round_streak(storage, **({"limit": window} if window else {}))
    if streak >= _SILENT_ROUNDS and _db_has_listings(storage):
        alerts.append(Alert(
            key="silent_rounds",
            level=LEVEL_DOWN,
            title="⛔ 全局连续零房源",
            body=f"连续 {streak} 轮全部 source 合计 0 条｜库内仍有房源记录",
        ))

    return alerts


def _db_has_listings(storage) -> bool:
    """库里是否有过房源。全局零房源告警的前提。

    和单 source 的 ``max_listings > 0`` 是同一个思路——「抓到 0 条」只有相对
    某个基线才有意义。但这里的基线**不能取判定窗口**：窗口只有二十来轮（约 2
    小时），2026-06-13 那次静默停摆持续了 7 周，两小时后窗口内全是零，告警就会
    自己闭嘴——恰恰在最需要它出声的时候。

    listings 表不会因为抓不到就清空（stale 收敛只改状态、不删行），所以它是个
    不随故障时长衰减的基线。
    """
    try:
        return storage.count_all() > 0
    except Exception:
        return False


# ── 节流 + 恢复 ──────────────────────────────────────────────────────


def _load_active(storage) -> set[str]:
    try:
        raw = storage.get_meta(_ACTIVE_META_KEY, default="")
        return set(json.loads(raw)) if raw else set()
    except Exception:
        return set()


def _save_active(storage, keys: set[str]) -> None:
    try:
        storage.set_meta(_ACTIVE_META_KEY, json.dumps(sorted(keys)))
    except Exception:
        logger.debug("watchdog 活跃集写入失败（已忽略）", exc_info=True)


def _should_fire(storage, key: str, *, now: float) -> bool:
    """距上次发同一条告警是否已超过重发间隔。

    "从未发过" 必须显式返回 True，不能靠 ``now - 0 >= interval`` 顺带成立——
    那在真实时钟下碰巧为真，但把首次触发的正确性绑在了 ``time.time()`` 的量级上。
    """
    try:
        raw = storage.get_meta(_FIRED_META_PREFIX + key, default="")
    except Exception:
        return True
    if not raw:
        return True          # 从未发过（或恢复后被清空）
    try:
        last = float(raw)
    except (TypeError, ValueError):
        return True
    return now - last >= _REPEAT_INTERVAL


def _mark_fired(storage, key: str, *, now: float) -> None:
    try:
        storage.set_meta(_FIRED_META_PREFIX + key, str(now))
    except Exception:
        logger.debug("watchdog 节流时间戳写入失败（已忽略）", exc_info=True)


def _clear_fired(storage, key: str) -> None:
    try:
        storage.set_meta(_FIRED_META_PREFIX + key, "")
    except Exception:
        pass


def poll(storage, *, window: int | None = None, now: float | None = None) -> list[Alert]:
    """跑规则 + 节流 + 恢复检测，返回**本轮真正该发出去**的告警。

    调用方只需把返回的每条发给 admin；状态推进已经在这里做完了。
    异常一律吞掉并返回空列表——告警机制本身不该把 monitor 带崩。
    """
    now_ts = time.time() if now is None else now
    try:
        current = evaluate(storage, window=window)
    except Exception:
        logger.debug("watchdog 规则求值失败（已忽略）", exc_info=True)
        return []

    try:
        current_keys = {a.key for a in current}
        previous_keys = _load_active(storage)

        out: list[Alert] = []

        # 恢复：上轮在、这轮不在。恢复不节流——它是终止信号，只会发一次
        # （key 已从活跃集移除），而且正是用户最想立刻知道的那条。
        for key in sorted(previous_keys - current_keys):
            _clear_fired(storage, key)
            out.append(Alert(
                key=key,
                level=LEVEL_RECOVERED,
                title=f"✅ 已恢复：{key}",
                body="该告警条件已不再满足。",
            ))

        for a in current:
            if _should_fire(storage, a.key, now=now_ts):
                _mark_fired(storage, a.key, now=now_ts)
                out.append(a)

        _save_active(storage, current_keys)
        return out
    except Exception:
        logger.debug("watchdog 状态推进失败（已忽略）", exc_info=True)
        return []


def snapshot(storage, *, window: int | None = None) -> list[dict[str, Any]]:
    """当前活跃告警（只读，不推进任何状态）。面板用。"""
    try:
        return [
            {"key": a.key, "level": a.level, "title": a.title, "body": a.body}
            for a in evaluate(storage, window=window)
        ]
    except Exception:
        return []
