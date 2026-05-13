"""预登录 session 缓存管理。

流程背景
--------
try_book() 每次调用都需登录 Holland2Stay，多用户场景下每轮浪费 N 次登录。
本模块在每轮 scrape 前批量建立/刷新预登录 session，try_book() 直接复用，
命中时每次预订省去 ~1.5s 登录延迟。

缓存策略
--------
- 命中：session 存在 + email 一致 + token TTL 余量 > 5 min → 复用
- 未命中：在 executor 线程中异步刷新（与 scrape 并行）
- 失效：用户被禁用 / 移除自动预订 / 更改 email → 清理
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from booker import PrewarmedSession, create_prewarmed_session

if TYPE_CHECKING:
    from users import UserConfig

logger = logging.getLogger("monitor")

_TOKEN_REFRESH_MARGIN = 300  # TTL 余量阈值（秒）


class PrewarmCache:
    """预登录 session 缓存（单例，monitor 进程生命周期）。"""

    def __init__(self) -> None:
        self._cache: dict[str, "PrewarmedSession"] = {}

    # -- 查询 ----------------------------------------------------------

    def get(self, user_id: str) -> "PrewarmedSession | None":
        return self._cache.get(user_id)

    def is_valid(self, ps: "PrewarmedSession | None", expected_email: str) -> bool:
        """缓存命中需同时满足：session 存在 / email 一致 / TTL 余量充足。"""
        if ps is None:
            return False
        if ps.email != expected_email:
            return False
        return ps.token_expiry - time.monotonic() > _TOKEN_REFRESH_MARGIN

    def __contains__(self, user_id: str) -> bool:
        return user_id in self._cache

    def __len__(self) -> int:
        return len(self._cache)

    def keys(self):
        return self._cache.keys()

    # -- 写入 ----------------------------------------------------------

    def set(self, user_id: str, session: "PrewarmedSession") -> None:
        self._cache[user_id] = session

    def create(self, user: "UserConfig") -> "PrewarmedSession | None":
        """在 executor 线程中为单个用户建立预登录 session。失败返回 None。"""
        try:
            return create_prewarmed_session(
                user.auto_book.email, user.auto_book.password
            )
        except Exception as e:
            logger.warning(
                "[%s] 预登录失败 (%s)，下单时将回退到正常登录路径",
                user.name, e,
            )
            return None

    # -- 清理 ----------------------------------------------------------

    def invalidate(self, user_id: str) -> None:
        """移除并关闭指定用户的缓存 session（已不在缓存中时为 no-op）。"""
        ps = self._cache.pop(user_id, None)
        if ps:
            try:
                ps.session.close()
            except Exception:
                pass

    def clear(self) -> None:
        """关闭所有缓存的 session（热重载 / 进程退出时调用）。"""
        if not self._cache:
            return
        n = len(self._cache)
        for uid in list(self._cache.keys()):
            self.invalidate(uid)
        logger.info("已清理 %d 个 prewarm 缓存", n)
