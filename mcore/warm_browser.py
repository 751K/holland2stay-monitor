"""H2S 下单链路的常驻浏览器。

为什么要有它
------------
下单的关键路径本来是两件事拼起来的，成本差一个数量级：

    过 CF 挑战    16.5s（中位，480 次实测）   ← 属于**浏览器**
    NextAuth 登录 ~1.5s                      ← 属于**用户**

而 ``try_book`` 的每个调用都用 ``Authorization: Bearer <JWT>`` 认证，浏览器只是
一条过了 CF 的 HTTP 通道，和是谁登录的无关（见 booker.py 的 ``login`` 与
``_gql``）。也就是说贵的那一半是可以**所有用户共用一份**的。

2026-09-08 之前不是这么做的：预登录只在「本轮出现候选」时才启动，而候选的定义
就是「房源已经变成 Available」——所以预登录和抢房是同一瞬间开始的，每次都要现
过一次 CF。留存日志里快速通道触发 4 次、``prewarmed=no`` 4 次、成功 0 次，两次
有记录的失败(2026-08-27 代理 402、2026-09-08 CF 熔断)都死在这一步上，一次都没
走到过真正的下单。

所以这里常驻一个过了挑战的浏览器，把那 16.5 秒的赌局从关键路径上挪走——它在后台
按自己的节奏赌，输了可以慢慢重来；房源翻牌时手里已经有一条通的路，只剩 1.5 秒的
登录。

保活是必须的，不是优化
----------------------
``cf_clearance`` 标称一年是摆设，真正管事的是同域的 ``h2s_clr``，**0.5 小时**就
过期（见 browser_fetcher.BrowserFetcher 的类文档）。浏览器持续发请求时服务端会
一直续，所以能撑到 ``_BROWSER_MAX_AGE``（2 小时）；一旦闲着，半小时后这个"常驻"
就只剩一个空壳，下次用还是要现过挑战——那就白留了。因此 ``heartbeat()`` 必须被
按时调用。

并发
----
一个 BrowserFetcher 不能被多线程同时使唤，所以对外只给一把 ``RLock``，持有者在
真正发 I/O 的整段时间里握着它。后果是**多个用户同时下单会串行**——每笔下单的
GraphQL 序列是秒级，而实测可订窗口 p10 约 10 分钟，这个代价换得起。拿不到锁的
调用方回退到自己开浏览器，也就是 2026-09-08 之前的老路，不会更差。
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger("monitor")

def lane_enabled() -> bool:
    """这条常驻现在开着吗。**默认关。**

    为什么默认是关的
    ----------------
    2026-09-08 上线，2026-09-09 关掉。它在生产里泄漏浏览器：每次「就绪」5–6 分钟
    后 ``is_alive()`` 就判死，``_drop()`` 调了 ``close()`` 而 OS 进程并没有退——
    playwright 的 node 驱动和整棵 Chromium 进程树都还在。18 小时泄漏 5 套，容器
    从 1.14 GiB 涨到 1.73 GiB（上限 2 GiB），随后 xior 渲染器卡死 600 秒、h2s 过
    不了 CF 挑战并熔断。也就是说它没能加快下单，反而把两个 source 拖垮了。

    重新打开之前要先答出两个问题：浏览器为什么 5 分钟就死；``close()`` 收不掉时
    ``_drop()`` 怎么核实并强杀。**在那之前默认关。**

    开关放 RUNTIME_KEYS，可以从数据库注水、SIGHUP 热重载——救火用的开关如果关它
    本身要走一次部署，它在最需要的时候就是没用的。
    """
    import os

    return (os.environ.get("WARM_BROWSER_LANE") or "").strip().lower() in (
        "1", "true", "yes", "on")


#: 多久没发过请求就补一次探测。``h2s_clr`` 是 0.5 小时，取三分之一留足余量——
#: 保活本身极便宜（一个 GraphQL 探针），而漏一次的代价是整条常驻退化成冷启动。
_KEEPALIVE_INTERVAL = 600.0

#: 浏览器活到这个岁数就主动换掉。比 browser_fetcher 的 2 小时略短，好让重建发生
#: 在我们自己挑的时刻，而不是某次下单的中途。
_MAX_AGE = 6000.0

#: 重建失败后多久才允许再试。CF 正在拒绝时反复重建只会加深它的怀疑，而这条常驻
#: 本来就是「后台慢慢来」的东西，没有急的道理。
_REBUILD_COOLDOWN = 300.0


class WarmBrowserLane:
    """常驻的、过了 CF 的 H2S 浏览器。进程单例。"""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self._fetcher = None
        self._born_at = 0.0
        self._last_io = 0.0
        self._next_rebuild_at = 0.0

    # -- 状态 ----------------------------------------------------------

    @property
    def fetcher(self):
        """当前可用的 fetcher；没有就是 None。**调用方必须自己持 ``lock``。**"""
        return self._fetcher

    def stats(self) -> dict:
        """给日志和面板看的快照。"""
        now = time.monotonic()
        return {
            "up": self._fetcher is not None,
            "age": round(now - self._born_at, 1) if self._fetcher else 0.0,
            "idle": round(now - self._last_io, 1) if self._fetcher else 0.0,
        }

    def mark_used(self) -> None:
        """有人借着它发过请求了——保活计时归零。"""
        self._last_io = time.monotonic()

    # -- 维护 ----------------------------------------------------------

    def heartbeat(self, *, wanted: bool, may_rebuild: bool = True) -> None:
        """每轮调一次：该建的建、该续的续、该换的换。

        Parameters
        ----------
        wanted
            现在还需不需要这条常驻（没有启用自动预订的用户时为 False）。
            False 会把它关掉——留着一个没人用的 Chromium 只是白占内存，
            还在持续给 H2S 发探针。
        may_rebuild
            允许新建浏览器吗。登录链路被 CF 熔断期间传 False：**已经健康的
            照常保活**（它是我们唯一还通着的路，丢了就得重过挑战），但不要
            在人家正拒绝我们的时候去开新的。
        """
        if not wanted:
            self.close(reason="没有启用自动预订的用户")
            return

        # **非阻塞**。拿不到锁只有两种情况：另一次心跳正在建（建一次要十几秒，
        # 而轮次是 20 秒一跑，阻塞就会把心跳堆起来），或者有人正在下单——两种都
        # 轮不到这里做事。
        if not self.lock.acquire(blocking=False):
            return
        try:
            now = time.monotonic()

            if self._fetcher is not None:
                if not self._alive():
                    self._drop(f"浏览器已死：{self._death_reason()}")
                elif now - self._born_at > _MAX_AGE:
                    self._drop(f"到龄 {_MAX_AGE / 60:.0f} 分钟")

            if self._fetcher is None:
                if may_rebuild and now >= self._next_rebuild_at:
                    self._build()
                return

            if now - self._last_io >= _KEEPALIVE_INTERVAL:
                self._keepalive()
        finally:
            self.lock.release()

    def close(self, *, reason: str = "") -> None:
        with self.lock:
            if self._fetcher is not None:
                self._drop(reason or "主动关闭")

    # -- 内部 ----------------------------------------------------------

    def _alive(self) -> bool:
        try:
            return bool(self._fetcher.is_alive())
        except Exception:
            return False

    def _death_reason(self) -> str:
        """它到底是哪一处不对了。

        原来只记「浏览器已死」，于是 2026-09-09 排查时无从下手——是页面被关了、
        驱动断了、还是 Chromium 被 OOM 干掉了，三种原因的处置完全不同，而日志把
        它们写成同一句话。
        """
        f = self._fetcher
        try:
            if getattr(f, "_browser", None) is None:
                return "浏览器对象为空"
            page = getattr(f, "_page", None)
            if page is None:
                return "页面对象为空"
            if page.is_closed():
                return "页面已被关闭"
            return "连接已断开（驱动或 Chromium 没了）"
        except Exception as e:
            return f"连状态都问不出来（{type(e).__name__}: {e}）"

    def _drop(self, reason: str) -> None:
        logger.info("下单常驻浏览器关闭（%s）", reason)
        try:
            self._fetcher.close()
        except Exception:
            # **WARNING 不是 DEBUG。** 2026-09-09 那次泄漏里，close() 到底抛没抛
            # 是关键线索，而它被记在 DEBUG 上——线上是 INFO，等于什么都没记。
            # 一条「关不掉」正是「进程还在」的直接征兆，不该藏起来。
            logger.warning("关闭常驻浏览器时抛异常（进程可能还在）", exc_info=True)
        self._fetcher = None
        self._born_at = 0.0
        self._last_io = 0.0

    def _build(self) -> None:
        from config import CLOAKBROWSER_HEADLESS

        from browser_fetcher import BrowserFetcher

        t0 = time.monotonic()
        f = BrowserFetcher(headless=CLOAKBROWSER_HEADLESS)
        try:
            f.__enter__()
            f.ensure_initialized()          # 这里面是那 16.5 秒
        except Exception as e:
            try:
                f.close()
            except Exception:
                pass
            self._next_rebuild_at = time.monotonic() + _REBUILD_COOLDOWN
            # WARNING 不是 ERROR：这条常驻建不起来只是退回老路（下单时现过挑战），
            # 不是故障。真正的故障会在下单那条链路上自己报出来。
            logger.warning(
                "下单常驻浏览器建立失败，%.0f 分钟内不再重试（下单将退回现开浏览器）: %s",
                _REBUILD_COOLDOWN / 60, e,
            )
            return

        self._fetcher = f
        self._born_at = self._last_io = time.monotonic()
        logger.info("下单常驻浏览器就绪，过挑战耗时 %.1fs", self._born_at - t0)

    def _keepalive(self) -> None:
        """发一次 clearance 探针，把 ``h2s_clr`` 续上。

        用探针而不是随便一个请求：它是 profile 自己声明的那一个，和初始化最后
        一步用的是同一条（``_wait_for_clearance``），所以「探针过了」等价于
        「这条常驻现在真的能用」——保活和健康检查是同一件事，不用做两遍。
        """
        try:
            ok = self._fetcher.probe_clearance()
        except Exception as e:
            ok, reason = False, str(e)
        else:
            reason = "探针未通过"
        if not ok:
            # 保活失败 = 这条常驻已经不通了。**必须丢掉**：留着它，下单会拿到一个
            # 看起来健康、实际每个请求都 403 的 fetcher，而那比没有更糟——老路至少
            # 会自己重过挑战。
            self._drop(f"保活失败（{reason}）")
            return
        self._last_io = time.monotonic()


#: 进程单例。
warm_lane = WarmBrowserLane()
