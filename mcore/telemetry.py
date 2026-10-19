"""轮次遥测：把每个 source 本轮的结果写进 ``round_stats``。

从 monitor.py 拆出。日志通道仍用 ``monitor``——面板、告警和测试都按这个
logger 名字过滤，改名属于另一个变更。
"""
from __future__ import annotations

import logging
import time

from storage import Storage

logger = logging.getLogger("monitor")


def completeness_stats(completeness: dict[str, bool]) -> tuple[int, int]:
    """把 completeness 字典压成 (完整数, 总数)，用于轮次遥测。"""
    return sum(1 for ok in completeness.values() if ok), len(completeness)


def record_source_round(
        storage: Storage,
        *,
        round_at: str,
        source: str,
        listings: int = 0,
        targets: int = 0,
        complete: int = 0,
        started_at: float | None = None,
        error: BaseException | None = None,
        error_type: str = "",
        error_msg: str = "",
        total_targets: int = 0,
) -> None:
    """给 ``round_stats`` 记一行。

    每个 source 跑完就立即写，不攒到整轮结束——「整轮全失败」恰恰是最该留痕的
    情形，而那条路径会直接上抛，攒着的记录就丢了。

    失败可以用 ``error=`` 传异常（类名即 error_type），也可以用
    ``error_type=`` / ``error_msg=`` 直接写——后者是给「没有异常对象、但确实
    没抓成」的情形用的，比如熔断期跳过。

    storage 写失败不上抛（``record_round_stat`` 内部已吞），这里再兜一层是防
    round_at 之类的参数构造本身出错。观测不该把被观测的东西弄崩。
    """
    try:
        duration_ms = (
            int((time.monotonic() - started_at) * 1000) if started_at is not None else 0
        )
        if error is not None:
            error_type = error_type or type(error).__name__
            error_msg = error_msg or str(error)
        storage.record_round_stat(
            round_at=round_at,
            source=source,
            listings=listings,
            targets=targets,
            complete=complete,
            duration_ms=duration_ms,
            error_type=error_type,
            error_msg=error_msg,
            total_targets=total_targets,
        )
    except Exception:
        logger.debug("轮次遥测记录失败（已忽略）", exc_info=True)
