"""通知节流的判定函数。

节流状态本身在 ``mcore.state.runtime.throttles``；这里只提供按间隔 claim 的
薄封装。日志通道仍用 ``monitor``。
"""
from __future__ import annotations

import logging

from mcore.state import (
    BLOCK_NOTIFY_INTERVAL,
    H2S_LONG_BLOCK_NOTIFY_INTERVAL,
    INTERNAL_NOTIFY_INTERVAL,
    MAINTENANCE_NOTIFY_INTERVAL,
    OPERATION_REJECTED_NOTIFY_INTERVAL,
    PROXY_NOTIFY_INTERVAL,
    runtime,
)

logger = logging.getLogger("monitor")


def _storage():
    return runtime.throttle_storage


def should_notify_block() -> bool:
    """是否该发屏蔽通知。30 分钟最多一次，避免持续屏蔽时刷屏。"""
    return runtime.throttles["block"].claim(
        BLOCK_NOTIFY_INTERVAL, storage=_storage())


def should_notify_operation_rejected() -> bool:
    """operation 被上游拒时是否该告警。6 小时最多一次，理由见常量注释。"""
    return runtime.throttles["operation_rejected"].claim(
        OPERATION_REJECTED_NOTIFY_INTERVAL, storage=_storage())


def should_notify_h2s_long_block() -> bool:
    """H2S 长时间 403 后是否该通知 admin。6 小时最多一次。"""
    return runtime.throttles["h2s_long_block"].claim(
        H2S_LONG_BLOCK_NOTIFY_INTERVAL, storage=_storage())


def should_notify_maintenance() -> bool:
    """是否该给 admin 发维护通知。1 小时最多一次。"""
    return runtime.throttles["maintenance"].claim(
        MAINTENANCE_NOTIFY_INTERVAL, storage=_storage())


def should_notify_proxy() -> bool:
    """是否该给 admin 发代理失效通知。30 分钟最多一次。"""
    return runtime.throttles["proxy"].claim(
        PROXY_NOTIFY_INTERVAL, storage=_storage())


def should_notify_internal() -> bool:
    """是否该给 admin 发未分类/管线内部错误通知。30 分钟最多一次。"""
    return runtime.throttles["internal"].claim(
        INTERNAL_NOTIFY_INTERVAL, storage=_storage())
