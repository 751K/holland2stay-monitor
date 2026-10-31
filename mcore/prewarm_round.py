"""候选轮次的 prewarm：常驻浏览器心跳、按候选预登录、future 收尾。

从 monitor.run_once 的嵌套函数提出来。策略不变：先完成只读 scrape + diff，
确认本轮确实有 H2S 可订候选后，只给被分配到候选的用户准备预登录；没有候选
的轮次完全不碰 H2S 登录接口。日志通道仍用 ``monitor``。
"""
from __future__ import annotations

import asyncio
import functools
import logging
import os

from scrapers import BlockedError
from mcore.h2s_guard import (
    h2s_login_suppressed_remaining,
    mark_h2s_login_blocked,
)
from mcore.state import runtime
from mcore.booking_worker import get_booking_worker, existing_booking_worker, prune_booking_workers, RemoteSession

logger = logging.getLogger("monitor")

#: 候选轮次最多等 prewarm 多久，避免额外正常登录。
PREWARM_CANDIDATE_WAIT_SEC = 2.0
PREWARM_WORKER_TIMEOUT_SEC = float(os.environ.get("PREWARM_WORKER_TIMEOUT_SEC") or "120")


class PrewarmRound:
    def __init__(self, loop, storage, user_notifiers, dry_run: bool) -> None:
        self.loop = loop
        self.storage = storage
        self.user_notifiers = user_notifiers
        self.dry_run = dry_run
        #: 命中：同步可用
        self.cached: dict[str, object] = {}
        #: 未命中：后台刷新
        self.futures: dict[str, asyncio.Future] = {}

    def heartbeat(self) -> None:
        """在用户专属工作进程维护常驻浏览器，调度线程不会阻塞事件循环。

        心跳只保活浏览器，不登录；开关关闭时让已有工作进程关闭常驻浏览器。
        工作进程忙于预登录或预订时跳过心跳，不排队阻塞关键路径。
        """
        if self.dry_run:
            return
        from mcore.warm_browser import lane_enabled

        # 总开关关着时 wanted=False，heartbeat 会把已经建起来的那条关掉——不是
        # 「不再维护」而是「收干净」。留一个没人维护的 Chromium 在那儿，正是
        # 2026-09-09 那次泄漏的形状。
        wanted = lane_enabled() and any(
            u.auto_book.enabled and u.auto_book.email and u.auto_book.password
            for u, _ in self.user_notifiers
        )
        # 登录被 CF 熔断期间不新建：正在被拒绝的时候去开浏览器，只会给同一个出口
        # IP 再添几条失败记录。但**已经健康的那条要继续保活**——它是我们眼下唯一
        # 还通着的路，丢了就得等熔断结束再从头过挑战。
        may_rebuild = h2s_login_suppressed_remaining() <= 0

        def _done(fut) -> None:
            # 不 await 的 future 会把异常吞进垃圾回收。这里读一次 exception()，
            # 让它至少留下一行日志。
            exc = fut.exception()
            if exc is not None:
                logger.warning("常驻浏览器心跳异常（已忽略，下单会退回现开浏览器）",
                               exc_info=exc)

        first = next((u for u, _ in self.user_notifiers
                      if u.auto_book.enabled and u.auto_book.email and u.auto_book.password), None)
        if first is None:
            return
        worker = get_booking_worker(first.id) if wanted else existing_booking_worker(first.id)
        if worker is None or (not wanted and worker.process is None):
            return
        fut = self.loop.run_in_executor(
            None,
            functools.partial(worker.run, "heartbeat",
                              {"wanted": wanted, "may_rebuild": may_rebuild},
                              timeout=PREWARM_WORKER_TIMEOUT_SEC),
        )
        fut.add_done_callback(_done)

    def start_for_candidates(self, candidate_user_ids: set[str]) -> None:
        """只为本轮实际有 H2S 自动预订候选的用户启动预登录。"""
        if self.dry_run:
            return
        suppressed = h2s_login_suppressed_remaining()
        if suppressed > 0:
            logger.warning(
                "跳过 H2S prewarm：登录/预订 403 抑制窗口仍剩 %d 秒",
                suppressed,
            )
            return

        # 1) 失效不再合格的缓存（用户被禁用 / 移除自动预订 / 删除账号）
        active_user_ids = set()
        for user, _ in self.user_notifiers:
            ab = user.auto_book
            if ab.enabled and ab.email and ab.password:
                active_user_ids.add(user.id)
        for stale_uid in set(runtime.prewarm_cache.keys()) - active_user_ids:
            runtime.prewarm_cache.invalidate(stale_uid)
        prune_booking_workers(active_user_ids)

        if not candidate_user_ids:
            return

        # 2) 对合格用户：命中复用，未命中提交刷新。
        # 给每个 future 加 done_callback：完成后自动写入 prewarm_cache，
        # 确保慢 future（跨轮完成）的 session 不会泄漏。
        def _on_prewarm_done(user_id: str, fut) -> None:
            try:
                ps = fut.result()
            except BlockedError as e:
                # 这里原本也是永远不会命中的 BookingBlockedError。
                # OperationNotAllowedError 不该被接住。
                mark_h2s_login_blocked(e, self.storage)
                ps = None
            except Exception:
                ps = None
            if ps:
                runtime.prewarm_cache.set(user_id, ps)

        lane_offered = False
        for user, _ in self.user_notifiers:
            if user.id not in active_user_ids:
                continue
            if user.id not in candidate_user_ids:
                continue
            cached = runtime.prewarm_cache.get(user.id)
            if runtime.prewarm_cache.is_valid(cached, user.auto_book.email):
                self.cached[user.id] = cached
            else:
                if cached:
                    runtime.prewarm_cache.invalidate(user.id)
                # 常驻浏览器**只给本轮优先级最高的那个需要它的用户**。
                #
                # 剩下的人照旧自己开浏览器——也就是改动之前所有人的行为，没人变
                # 差。让他们排队等这一条共用通道才是错的：那个队列在改动之前根本
                # 不存在（每个用户各开各的，完全并行），而且 threading 的锁是随
                # 便唤醒一个等待者的，既不 FIFO 更不认 sort_order。用户排序是用
                # 户明确配的东西，不该被一把锁的唤醒顺序悄悄改写。
                #
                # user_notifiers 本身就是 sort_order 序（list_user_config_rows
                # 的 ORDER BY），所以「循环里第一个」就是「优先级最高的」。
                may_borrow = not lane_offered and not runtime.prewarm_cache.lane_in_use()
                lane_offered = lane_offered or may_borrow
                worker = get_booking_worker(user.id)
                def _remote_prewarm(u=user, w=worker, borrow=may_borrow):
                    email, created, expires = w.run(
                        "prewarm", {"email": u.auto_book.email,
                                    "password": u.auto_book.password, "may_borrow": borrow,
                                    "name": u.name, "user_id": u.id},
                        timeout=PREWARM_WORKER_TIMEOUT_SEC)
                    return RemoteSession(w, email, created, expires)
                fut = self.loop.run_in_executor(None, _remote_prewarm)
                self.futures[user.id] = fut
                fut.add_done_callback(
                    lambda f, uid=user.id: self.loop.call_soon_threadsafe(
                        _on_prewarm_done, uid, f
                    )
                )

        if self.cached or self.futures:
            logger.debug(
                "prewarm 状态: 命中 %d / 刷新 %d",
                len(self.cached), len(self.futures),
            )

    async def wait_for_candidates(self) -> None:
        """候选轮次给 prewarm 最多 2 秒赶上快速通道，避免额外正常登录。"""
        if not self.futures:
            return
        done, pending = await asyncio.wait(
            list(self.futures.values()),
            timeout=PREWARM_CANDIDATE_WAIT_SEC,
        )
        if done or pending:
            logger.debug(
                "candidate prewarm 等待 %.1fs：完成 %d / 未完成 %d",
                PREWARM_CANDIDATE_WAIT_SEC,
                len(done),
                len(pending),
            )

    async def stash_pending(self) -> None:
        """收集已完成的 prewarm future，供本轮 booking 快速通道使用。
        未完成的 future 由 done_callback 兜底——完成后自动写入缓存。"""
        for user_id, fut in list(self.futures.items()):
            if not fut.done():
                continue
            try:
                ps = fut.result()
            except BlockedError as e:
                # 这里原本也是永远不会命中的 BookingBlockedError。
                # OperationNotAllowedError 不该被接住。
                mark_h2s_login_blocked(e, self.storage)
                ps = None
            except Exception:
                ps = None
            if ps:
                runtime.prewarm_cache.set(user_id, ps)
            del self.futures[user_id]
