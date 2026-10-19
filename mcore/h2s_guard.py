"""H2S 的熔断与登录抑制。

熔断本身已推广成 per-source（``mcore/circuit.py``），这里只剩 H2S 独有的
那一半：登录/预订链路抑制——只有 H2S 有自动预订，403 之后继续碰登录接口只会
让 WAF 状态更热。日志通道仍用 ``monitor``。
"""
from __future__ import annotations

import logging

from scrapers import BlockedError
from mcore.state import H2S_LOGIN_BLOCKED_SUPPRESS_SEC, runtime

logger = logging.getLogger("monitor")

H2S_SOURCE = "holland2stay"


def h2s_login_suppressed_remaining() -> int:
    """H2S 登录/预订链路是否因 403 被临时抑制；返回剩余秒数。"""
    return runtime.h2s_login_block.remaining()


def mark_h2s_login_blocked(reason: BaseException | str, storage=None) -> None:
    """H2S 登录/预订遇到 Cloudflare 403 后，短期内停止触碰登录链路。

    ``storage`` 给了才落库。少数调用点（预订结果回调）拿不到 storage，那里退化
    成进程内抑制——比原来强，但不跨重启。
    """
    runtime.h2s_login_block.open(
        H2S_LOGIN_BLOCKED_SUPPRESS_SEC, reason=str(reason), storage=storage,
    )
    runtime.prewarm_cache.clear()
    logger.warning(
        "🚫 H2S 登录/预订遇到 Cloudflare WAF，未来 %d 秒内暂停登录链路: %s",
        H2S_LOGIN_BLOCKED_SUPPRESS_SEC,
        reason,
    )


def h2s_circuit_remaining() -> int:
    """H2S 抓取 circuit breaker 剩余暂停秒数。"""
    return runtime.source_circuits.remaining(H2S_SOURCE)


def mark_h2s_scrape_blocked(reason: BaseException | str, storage=None) -> int:
    """H2S 抓取被 Cloudflare 403 后，打开熔断 + 抑制登录链路。

    熔断本身已经推广成 per-source（``mcore/circuit.py``），这里只剩 H2S 独有的
    那一半：**登录/预订链路抑制**。那是因为只有 H2S 有自动预订，而 403 之后继续
    去碰登录接口只会让 WAF 状态更热。
    """
    exc = reason if isinstance(reason, BaseException) else BlockedError(str(reason))
    cooldown = runtime.source_circuits.trip(H2S_SOURCE, exc, storage=storage)
    mark_h2s_login_blocked(reason, storage=storage)
    return cooldown


def mark_h2s_scrape_recovered(storage=None) -> None:
    """H2S canary 成功后关闭熔断 + 解除登录抑制，下一轮恢复完整 H2S 抓取。"""
    runtime.source_circuits.recover(H2S_SOURCE, storage=storage)
    runtime.h2s_login_block.reset(storage)
