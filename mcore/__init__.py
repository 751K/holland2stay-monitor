"""mcore — monitor.py 核心逻辑提取

本包存放从 monitor.py 抽离的纯逻辑和小型服务类，
目标是降低 monitor.py 的体量并让关键流程可独立测试。

模块
----
interval  智能轮询间隔计算（纯函数）
prewarm   预登录 session 缓存管理
booking   自动预订 + 竞争失败重试队列
"""

from mcore.interval import apply_jitter, get_interval  # noqa: F401
from mcore.booking import RetryQueue, area_key, book_with_fallback  # noqa: F401
from mcore.prewarm import PrewarmCache  # noqa: F401
