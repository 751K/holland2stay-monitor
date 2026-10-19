"""全面故障的告警节流、恢复判定与 admin 投递。

从 monitor.py 拆出。``OutageTracker`` 只做判定不做 IO，投递交给
``notify_admin_only``；日志通道仍用 ``monitor``。
"""
from __future__ import annotations

import logging
import time

from storage import Storage

logger = logging.getLogger("monitor")

#: 首次达阈值立即发，之后 15 / 30 / 60 分钟递增，封顶 1 小时：全面宕机要立刻知道，
#: 但连续 5 小时不该收 20 条。
OUTAGE_ALERT_BACKOFF = (900, 1800, 3600)


class OutageTracker:
    """全面故障的告警节流与恢复判定。

    只做判定不做 IO，投递交给调用方——这样测试不必搭 push / web_notifier 环境。
    时间由调用方传入（``time.monotonic()``），测试可直接推进。
    """

    def __init__(self) -> None:
        self._started_at: float | None = None
        self._alerts = 0
        self._last_alert_at = 0.0
        self._rounds = 0

    @property
    def active(self) -> bool:
        return self._started_at is not None

    @property
    def rounds(self) -> int:
        """本次故障已经失败了多少轮。"""
        return self._rounds

    def elapsed(self, now: float) -> float:
        """本次故障已持续多少秒；不在故障中时为 0。"""
        return 0.0 if self._started_at is None else now - self._started_at

    def record_failure(self, now: float) -> bool:
        """记一次全面失败，返回本次是否该发告警。

        **首次必发。** 调用方只在确认是全面故障时才调进来（网络分支要连续 3 轮、
        屏蔽分支一轮就是 15 分钟起步的冷却），门槛已经在外面把住了；这里再压一层
        「先观察几轮」只会推迟通知。恢复告警靠这条性质成立：进入过故障态就一定
        通知过，不会出现凭空一条「已恢复」。
        """
        if self._started_at is None:
            self._started_at = now
            self._alerts = 0
            self._last_alert_at = 0.0
            self._rounds = 0
        self._rounds += 1
        if self._alerts:
            idx = min(self._alerts - 1, len(OUTAGE_ALERT_BACKOFF) - 1)
            if now - self._last_alert_at < OUTAGE_ALERT_BACKOFF[idx]:
                return False
        self._alerts += 1
        self._last_alert_at = now
        return True

    def record_success(self, now: float) -> tuple[float, int] | None:
        """记一次成功。返回 ``(故障持续秒数, 失败轮数)``；本来就没在故障中则 None。

        不在故障中时**必须**返回 None：否则每一轮正常抓取都会发一条「已恢复」。
        """
        if self._started_at is None:
            return None
        span, rounds = now - self._started_at, self._rounds
        self._started_at = None
        self._alerts = 0
        self._rounds = 0
        return span, rounds


#: 模块级单例。进程重启后清零——重启后若故障仍在，第一轮会重新告警，符合预期。
outage = OutageTracker()


def format_duration(seconds: float) -> str:
    """把秒数写成「N 小时 M 分钟」。告警里「5 小时 5 分钟」比「18300 秒」有用。"""
    total = int(seconds)
    h, m = divmod(total // 60, 60)
    if h:
        return f"{h} 小时 {m} 分钟"
    return f"{m} 分钟" if m else f"{total} 秒"


async def notify_admin_only(
    storage: Storage,
    web_notifier,
    msg: str,
    *,
    kind: str,
) -> None:
    """把一条错误只发给 admin（web 面板 + admin push），不打扰普通用户。

    用于普通用户无法采取行动的内部/系统级错误（代理失效、数据库/通知管线
    故障、未分类异常）。两个渠道各自吞异常——告警通道本身不该再把 run_once
    带崩。"""
    if web_notifier:
        try:
            await web_notifier.send_error(msg)
        except Exception:
            logger.debug("admin web 告警发送失败（已忽略）", exc_info=True)
    from mcore import push as _push
    try:
        await _push.dispatch_admin(storage, msg, kind=kind)
    except Exception:
        logger.debug("admin push 告警发送失败（已忽略）", exc_info=True)


async def alert_outage(
    storage: Storage,
    web_notifier,
    summary: str,
    detail: str,
) -> None:
    """全面故障告警。只发 admin——用户对代理/网络故障无从处置。"""
    msg = (
        f"⛔ 全面抓取故障：{summary}\n\n"
        f"已持续 {format_duration(outage.elapsed(time.monotonic()))}，"
        f"连续 {outage.rounds} 轮所有 source 均失败。\n\n"
        f"{detail}\n\n"
        "监控进程仍在运行并按退避重试；恢复后会再发一条通知。"
    )
    await notify_admin_only(storage, web_notifier, msg, kind="outage")


async def alert_outage_recovered(
    storage: Storage,
    web_notifier,
    span: float,
    rounds: int,
) -> None:
    """恢复告警。kind 与故障告警不同，否则会被 push 的 dedup 压掉。"""
    msg = (
        f"✅ 抓取已恢复\n\n"
        f"本次全面故障持续 {format_duration(span)}，期间 {rounds} 轮全部失败。"
    )
    await notify_admin_only(storage, web_notifier, msg, kind="outage_recovered")
