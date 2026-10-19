"""抓取后管线：diff → 影子过滤 → 重放 → 候选 → 预订 → 通知 → 推送 → 持久化。

从 monitor.run_once 拆出。``run_once`` 只负责抓取阶段和失败分支；这一段在抓取
成功之后运行，自带管线异常兜底。日志通道仍用 ``monitor``。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from mcore.booking_candidates import collect_booking_candidates, listing_source
from mcore.booking_round import process_booking_results, submit_bookings
from mcore.notify_round import (
    drop_shadow_sources,
    merge_pending_events,
    notify_new_listings,
    notify_status_changes,
)
from mcore.outage import notify_admin_only
from mcore.state import runtime
from mcore.throttles import should_notify_internal
from storage import Storage

logger = logging.getLogger("monitor")


def clear_maintenance_meta_if_recovered(storage: Storage) -> None:
    """抓取成功后，若之前处于维护态则清掉 maintenance meta，让 dashboard banner 消失。

    写 ended_at 留个最近一次恢复时间戳便于排查；seen_at 清空即"不在维护中"。"""
    if storage.get_meta("upstream_maintenance_seen_at", default=""):
        storage.set_meta(
            "upstream_maintenance_ended_at",
            datetime.now(timezone.utc).isoformat(),
        )
        storage.set_meta("upstream_maintenance_seen_at", "")
        storage.set_meta("upstream_maintenance_last_at", "")
        logger.info("🔧→✅ H2S 平台维护已结束，抓取恢复正常")


async def run_post_scrape(
    cfg,
    storage,
    user_notifiers,
    fresh,
    completeness,
    *,
    web_notifier,
    dry_run: bool,
    booking_deadline: float,
    loop,
    prewarm,
):
    # ── 抓取后管线：对比入库 → 预订 → 通知 → 推送 ─────────────────── #
    # 这段过去裸跑在 run_once 顶层，没有任何 except——一旦 storage.diff() /
    # set_meta() / 通知分发抛异常（DB 锁死、磁盘满、schema 损坏等），会一路
    # 冒泡到 main_loop 的 generic except，只打日志、不通知任何人，等于监控
    # 静默瘫痪。这里统一兜底：归类为"数据/通知管线错误"，给 admin 发一条
    # 带类型的告警（30 min 节流），然后 return {} 走正常间隔——避免 re-raise
    # 触发 main_loop 的 10s 紧重试反复重新抓站。
    try:
        new_listings, status_changes = storage.diff(fresh)

        # ── 重放上一轮没发出去的事件 ─────────────────────────────────── #
        # diff() 检测变更的副作用就是覆盖掉用来检测的旧状态，所以「diff 已提交、
        # 通知还没发」这个窗口里进程一死，事件就永久丢了——下一轮 diff 看到
        # old_status == new_status，什么都不产出。触发条件很日常：2026-08-20
        # 一天之内部署了 12 次，每次 --force-recreate 都在打断正在跑的轮次。
        #
        # 这里把 notified=0 的事件捞回来（有时间窗、有条数上限，见
        # mstorage/_listings.py），交付语义从 at-most-once 变成 at-least-once。
        #
        # **重复通知只是打扰，漏掉通知会让人错过房子** —— 代价不对称，所以选前者。
        if not dry_run:
            try:
                replayed = merge_pending_events(
                    storage, new_listings, status_changes,
                )
                if replayed:
                    logger.warning(
                        "重放 %d 条上一轮未发出的通知事件"
                        "（上次多半是部署或崩溃打断了通知阶段）", replayed,
                    )
                storage.retire_stale_pending()
            except Exception:
                # 重放是补偿机制，它自己坏掉不该拖垮正常通知
                logger.warning("未投递事件重放失败（本轮跳过）", exc_info=True)

        # ── 影子 source：入库了，但不通知 ─────────────────────────────── #
        # 新平台上线前的静默验证期：先确认它抓得对、数据长什么样，再决定是否
        # 对用户开放。**必须在 diff() 之后过滤**——diff 要照常执行，房源才会
        # 进库、状态变更才会被记录；被拦掉的只有「告诉谁」这一步。
        #
        # 也必须在**重放之后**过滤：重放会把上一轮崩溃前入库、通知阶段没走完
        # 的旧事件捞回来，它们同样可能属于影子 source。先重放再过滤，这些
        # 事件才会被一起拦下并标记；顺序反过来，它们会绕过影子保证直接投递。
        #
        # 副作用是这些 listing 的 notified 一直是 0。取消影子后不会补发历史
        # ——diff() 只对真正的新 id 产出 new_listings，老的不会再冒出来。
        # 这是想要的：解除影子不该给用户灌一堆积压通知。
        new_listings, status_changes = drop_shadow_sources(
            cfg, new_listings, status_changes, storage=storage,
        )

        # diff() 成功后再写时间戳，确保面板显示的 last_scrape_at 对应一次完整的
        # "抓取 + 入库" 操作；若 diff() 抛异常，时间戳不会被更新。
        # 计数用 fresh（含影子 source）——它回答的是「抓到多少」，不是「通知了多少」。
        storage.set_meta("last_scrape_at", datetime.now(timezone.utc).isoformat())
        storage.set_meta("last_scrape_count", str(len(fresh)))

        # 维护态恢复：本轮成功就清掉 maintenance meta，让 dashboard banner 消失。
        clear_maintenance_meta_if_recovered(storage)

        # ── 快速候选预扫描：纯内存收集候选，抢在发通知之前提交预订 ──────── #
        ab_candidates, status_transition = collect_booking_candidates(
            new_listings, status_changes, fresh, user_notifiers, storage,
        )
        candidate_user_ids = {
            uid for uid, candidates in ab_candidates.items()
            if any(listing_source(listing) == "holland2stay" for listing in candidates)
        }
        prewarm.heartbeat()
        prewarm.start_for_candidates(candidate_user_ids)
        await prewarm.wait_for_candidates()

        # ── 立即将 book_with_fallback() 提交到线程池（快速通道）──────── #
        # 新上线可预订 / 状态变更 → Available to book 均立即提交 run_in_executor，
        # 预订请求在发出通知之前就进入 Holland2Stay 服务器（节省 1-3 秒）。
        ab_futures = submit_bookings(
            loop, ab_candidates, user_notifiers,
            prewarm.cached, prewarm.futures, status_transition, booking_deadline,
            storage=storage,
        )

        # 没有候选的用户的 prewarm（如果是新刷新的）存入缓存供下轮复用
        await prewarm.stash_pending()

        # ── 新房源通知（预订已在后台线程并行运行）───────────────────── #
        # 标记策略：任意用户通知成功即标记为"已通知"。若部分用户渠道失败，
        # 该 listing 不会补发——实际业务中多用户同渠道很少部分失败。
        # APNs 推送：与现有 4 渠道并行，fire-and-forget；
        # 同一用户本轮匹配 ≥ push.aggregate_threshold() 套时改聚合一条。
        from mcore import push as _push  # 局部 import，避免冷启动加载 httpx

        total_notified, new_push = await notify_new_listings(
            new_listings, user_notifiers, web_notifier, storage, _push,
        )
        sc_push = await notify_status_changes(
            status_changes, user_notifiers, web_notifier, storage, _push,
        )
        booking_push = await process_booking_results(
            ab_futures, web_notifier, storage, _push,
        )
        push_tasks = [*new_push, *sc_push, *booking_push]

        # ── 等待本轮所有 APNs 推送完成 ───────────────────────────────── #
        # asyncio.create_task() 的 fire-and-forget 任务，在 run_once 返回前等齐；
        # 否则下一轮可能在 APNs 网络 IO 完成前重叠开始。各 task 自身吞异常。
        if push_tasks:
            try:
                results = await asyncio.gather(*push_tasks, return_exceptions=True)
                sent = sum(r for r in results if isinstance(r, int))
                errs = sum(1 for r in results if isinstance(r, Exception))
                logger.info("APNs 本轮推送结果: %d 设备成功 / %d 任务异常", sent, errs)
            except Exception:
                logger.exception("等待 push tasks 异常")

        # ── 持久化重试队列（仅在变更时写入）─────────────────────────── #
        runtime.retry_queue.save(storage)

        logger.info(
            "本轮结束: %d 新房源（已通知 %d），%d 状态变更，数据库共 %d 条",
            len(new_listings), total_notified, len(status_changes), storage.count_all(),
        )
        return completeness
    except Exception as e:
        # 抓取已成功、但入库/通知管线挂了。给 admin 一条带类型的告警，return {}
        # 走正常间隔，避免对已抓到的数据做无意义的 10s 紧重试。
        await prewarm.stash_pending()
        logger.error(
            "抓取后管线错误（入库/通知）listings=%d: %s",
            len(fresh), e, exc_info=True,
        )
        if should_notify_internal():
            await notify_admin_only(
                storage, web_notifier,
                f"数据/通知管线错误\n\n{type(e).__name__}: {e}\n\n"
                f"房源已抓到 {len(fresh)} 条，但写库或发通知阶段失败"
                f"（如数据库锁死/磁盘满）。请尽快查看服务器日志与磁盘。"
                f"30 分钟内不重复通知。",
                kind="error",
            )
        return {}
