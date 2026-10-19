"""进程级运行时状态的单一归属。

这些对象以前散落在 monitor.py 顶层，热重载、测试隔离和拆分都靠直接改模块
全局。现在收进 ``RuntimeState``：需要状态的地方拿同一个 ``runtime``，绑定
持久化句柄只需要调用一次 ``bind_storage``。

注意：``reload_event`` / ``h2s_wake_event`` 仍在 monitor.main_loop 里赋值，
等事件循环那一步再收进来；直接改成导入别名会让 ``global`` 赋值悄悄失效。
"""
from __future__ import annotations

from mcore.backoff import PersistedBackoff
from mcore.booking import RetryQueue
from mcore.circuit import SourceCircuits
from mcore.outage import outage as _outage
from mcore.pacing import AdaptivePacing
from mcore.prewarm import PrewarmCache

#: H2S 登录/预订 403 后 1 小时内不碰登录链路。
H2S_LOGIN_BLOCKED_SUPPRESS_SEC = 3600

# 通知节流间隔（秒）。
BLOCK_NOTIFY_INTERVAL = 1800
OPERATION_REJECTED_NOTIFY_INTERVAL = 21600
H2S_LONG_BLOCK_NOTIFY_INTERVAL = 21600
MAINTENANCE_NOTIFY_INTERVAL = 3600
PROXY_NOTIFY_INTERVAL = 1800
INTERNAL_NOTIFY_INTERVAL = 1800


class RuntimeState:
    """进程生命周期内的可变状态集合。

    字段都是**原地修改**的单例对象；除 ``throttle_storage`` 外没有重新赋值，
    因此外部持有引用是安全的。
    """

    def __init__(self) -> None:
        self.retry_queue = RetryQueue()
        self.prewarm_cache = PrewarmCache()
        self.source_circuits = SourceCircuits()
        self.source_pacing = AdaptivePacing()
        self.h2s_login_block = PersistedBackoff(
            "h2s_login_block", max_seconds=H2S_LOGIN_BLOCKED_SUPPRESS_SEC,
        )
        self.throttles = {
            "block": PersistedBackoff(
                "throttle_notify_block", max_seconds=BLOCK_NOTIFY_INTERVAL),
            "operation_rejected": PersistedBackoff(
                "throttle_notify_operation_rejected",
                max_seconds=OPERATION_REJECTED_NOTIFY_INTERVAL),
            "h2s_long_block": PersistedBackoff(
                "throttle_h2s_long_block",
                max_seconds=H2S_LONG_BLOCK_NOTIFY_INTERVAL),
            "maintenance": PersistedBackoff(
                "throttle_notify_maintenance",
                max_seconds=MAINTENANCE_NOTIFY_INTERVAL),
            "proxy": PersistedBackoff(
                "throttle_notify_proxy", max_seconds=PROXY_NOTIFY_INTERVAL),
            "internal": PersistedBackoff(
                "throttle_notify_internal", max_seconds=INTERNAL_NOTIFY_INTERVAL),
        }
        #: 供 _should_notify_* 落库用的 Storage 句柄。None = 只活在进程内。
        self.throttle_storage = None
        self.outage = _outage
        #: source -> 专属长存单线程 executor（见 monitor._get_browser_executor）。
        self.browser_executors: dict = {}

    def bind_storage(self, storage) -> None:
        """把落库句柄接上，并恢复所有退避 / 熔断 / 节流状态。"""
        self.throttle_storage = storage
        self.source_circuits.load(storage)
        self.source_pacing.load(storage)
        for b in (
            self.h2s_login_block,
            self.throttles["block"],
            self.throttles["operation_rejected"],
            self.throttles["h2s_long_block"],
            self.throttles["maintenance"],
            self.throttles["proxy"],
            self.throttles["internal"],
        ):
            try:
                b.load(storage)
            except Exception:
                import logging
                logging.getLogger("monitor").warning(
                    "恢复退避状态失败（按未退避处理）", exc_info=True)


runtime = RuntimeState()

#: 兼容旧调用点/测试：booking_candidates 直接从模块取重试队列。
retry_queue = runtime.retry_queue
