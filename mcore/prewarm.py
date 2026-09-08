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
from scrapers.base import BlockedError

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

    def lane_in_use(self) -> bool:
        """缓存里已经有人占着常驻浏览器了吗。

        同一时刻只让一个 session 绑常驻——两个都绑就会在下单时排队，而那个队列
        既不认用户优先级，也是改动之前根本不存在的东西（此前每个用户各开一个
        浏览器，完全并行）。
        """
        return any(not ps.owns_fetcher for ps in self._cache.values())

    def create(self, user: "UserConfig", *,
               may_borrow_lane: bool = True) -> "PrewarmedSession | None":
        """在 executor 线程中为单个用户建立预登录 session。

        返回 None = 这次没建成，下单时走正常登录路径。
        **BlockedError 例外，原样上抛**：调用方据此推进登录抑制窗口。
        压成 None 等于把「CF 挡了我们」降级成「这次没建成」，下一轮照样再撞一次。
        """
        try:
            return create_prewarmed_session(
                user.auto_book.email, user.auto_book.password,
                shared=self._borrow_lane() if may_borrow_lane else None,
            )
        except BlockedError:
            # CF 屏蔽要上抛，让调用方推进登录抑制窗口——**不能**说成「回退正常
            # 登录」：回退过去也是同一个 403，而且抑制生效后这一轮压根不下单。
            # 这条日志说错了很久，因为上抛的那半边此前根本没人接住
            # （见 monitor 里 except BlockedError 处的注释）。
            logger.warning(
                "[%s] 预登录遭 Cloudflare 屏蔽，上抛以暂停登录链路", user.name,
            )
            raise
        except (TypeError, AttributeError):
            # **这两类永远不是「这次没建成」，是代码本身错了**（签名对不上、
            # 属性没了）。和网络抖动混在一个 WARNING 里的后果，2026-09-08 在本仓
            # 自己的测试里就复现过一次：给 create_prewarmed_session 加了个参数，
            # 桩函数没跟上，于是每一轮都「预登录失败，回退正常登录路径」——系统
            # 照常跑、房子照常抓不到，日志读起来像 CF 又不高兴了。
            #
            # 仍然不上抛（一个笔误不该让整轮监控停摆），但要 ERROR + 堆栈，让它
            # 进 errors.log 和 /monitoring 面板，而不是混在噪音里。
            logger.error(
                "[%s] 预登录代码有误（不是网络问题），下单将一直退回正常登录路径",
                user.name, exc_info=True,
            )
            return None
        except Exception as e:
            logger.warning(
                "[%s] 预登录失败 (%s)，下单时将回退到正常登录路径",
                user.name, e,
            )
            return None

    # -- 清理 ----------------------------------------------------------

    @staticmethod
    def _borrow_lane():
        """常驻浏览器可用就借，返回 ``(fetcher, lock)``；不可用返回 None。

        这里**不加锁**：只读一个属性和 ``is_alive()``，后者按其文档只看本地标志
        位、不发任何 IPC。为这个去抢锁的话，就得排在别人整笔下单后面，而我们要
        判断的仅仅是「有没有这条常驻」。

        真正的互斥发生在用它的时候——``create_prewarmed_session`` 拿这把锁做登录，
        ``try_book`` 拿它做整笔下单。拿不到就等，登录只要 ~1.5 秒；等太久的话
        monitor 那边的 2 秒上限会先放弃，自动退回「自己开一个」的老路。
        """
        try:
            from mcore.warm_browser import warm_lane

            f = warm_lane.fetcher
            if f is None or not f.is_alive():
                return None
            return (f, warm_lane.lock)
        except Exception:
            return None

    def invalidate(self, user_id: str) -> None:
        """移除并关闭指定用户的缓存 session（已不在缓存中时为 no-op）。

        借来的常驻浏览器不关——``close_if_owned`` 负责区分。在这里无差别 close()
        会把全进程共用的那一个关掉，而调用方（用户被禁用、改邮箱）完全想不到自己
        顺手废掉了下单通道。
        """
        ps = self._cache.pop(user_id, None)
        if ps:
            ps.close_if_owned()

    def clear(self) -> None:
        """关闭所有缓存的 session（热重载 / 进程退出时调用）。"""
        if not self._cache:
            return
        n = len(self._cache)
        for uid in list(self._cache.keys()):
            self.invalidate(uid)
        logger.info("已清理 %d 个 prewarm 缓存", n)
