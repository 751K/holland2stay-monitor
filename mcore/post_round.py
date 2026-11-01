"""抓取后管线：diff → 重放 → 影子过滤 → 候选 → 预订 → 通知 → 推送 → 持久化。

从 monitor.run_once 拆出。``run_once`` 只负责抓取阶段和失败分支；这一段在抓取
成功之后运行，自带管线异常兜底。日志通道仍用 ``monitor``。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from dataclasses import dataclass, field

from models import Listing

from mcore.booking_candidates import collect_booking_candidates, listing_source
from mcore.booking_round import BookingJob, process_booking_results, submit_bookings
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


# 这些结果只在一轮内传递。通知列表可被重放合并，但阶段不会通过模块全局
# 交换本轮数据；出错时也能明确知道哪些预订已经提交。
StatusChange = tuple[Listing, str, str, int]


@dataclass
class RoundEvents:
    new_listings: list[Listing]
    status_changes: list[StatusChange]


@dataclass
class BookingSubmissions:
    futures: list[BookingJob] = field(default_factory=list)
    errors: list[Exception] = field(default_factory=list)


@dataclass
class DeliveryResult:
    total_notified: int
    errors: list[Exception]


def prepare_round_events(cfg, storage, fresh: list[Listing], *, dry_run: bool) -> RoundEvents:
    """入库并合并重放事件，之后过滤影子平台；入库失败则终止本轮。"""
    new_listings, status_changes = storage.diff(fresh)
    if not dry_run:
        try:
            replayed = merge_pending_events(storage, new_listings, status_changes)
            if replayed:
                logger.warning("重放 %d 条上一轮未发出的通知事件（上次可能被中断）", replayed)
            storage.retire_stale_pending()
        except Exception:
            logger.warning("未投递事件重放失败（本轮跳过）", exc_info=True)

    # 重放必须先于影子过滤，否则旧事件可能绕过影子平台的通知限制。
    new_listings, status_changes = drop_shadow_sources(
        cfg, new_listings, status_changes, storage=storage,
    )
    storage.set_meta("last_scrape_at", datetime.now(timezone.utc).isoformat())
    storage.set_meta("last_scrape_count", str(len(fresh)))
    clear_maintenance_meta_if_recovered(storage)
    return RoundEvents(new_listings, status_changes)


async def submit_round_bookings(
    events: RoundEvents, fresh: list[Listing], user_notifiers, storage,
    *, booking_deadline: float, loop, prewarm,
) -> BookingSubmissions:
    """收集候选、预登录并提交预订，返回所有已提交任务及提交错误。"""
    ab_candidates, status_transition = collect_booking_candidates(
        events.new_listings, events.status_changes, fresh, user_notifiers, storage,
    )
    candidate_user_ids = {
        uid for uid, candidates in ab_candidates.items()
        if any(listing_source(listing) == "holland2stay" for listing in candidates)
    }
    # 心跳需在候选预登录之前执行，且没有候选时也要执行。
    try:
        prewarm.heartbeat()
        prewarm.start_for_candidates(candidate_user_ids)
        await prewarm.wait_for_candidates()
    except Exception:
        logger.exception("预登录准备失败，本轮继续正常预订")

    submitted = BookingSubmissions()
    try:
        submit_bookings(
            loop, ab_candidates, user_notifiers,
            prewarm.cached, prewarm.futures, status_transition, booking_deadline,
            storage=storage, submitted=submitted.futures,
        )
    except Exception as exc:
        logger.exception("提交预订阶段失败，继续处理已提交任务")
        submitted.errors.append(exc)
    try:
        await prewarm.stash_pending()
    except Exception:
        logger.exception("收集预登录结果失败，继续处理预订和通知")
    return submitted


async def deliver_round_events(
    events: RoundEvents, bookings: BookingSubmissions, user_notifiers,
    storage, web_notifier, push,
) -> DeliveryResult:
    """分别处理房源、状态及预订通知，等待所有已返回的推送任务。"""
    errors = list(bookings.errors)
    total_notified = 0
    push_tasks = []
    try:
        total_notified, _ = await notify_new_listings(
            events.new_listings, user_notifiers, web_notifier, storage, push,
            task_sink=push_tasks,
        )
    except Exception as exc:
        logger.exception("新房源通知阶段失败，继续处理状态变更和预订结果")
        errors.append(exc)
    try:
        await notify_status_changes(
            events.status_changes, user_notifiers, web_notifier, storage, push,
            task_sink=push_tasks,
        )
    except Exception as exc:
        logger.exception("状态变更通知阶段失败，继续处理预订结果")
        errors.append(exc)
    try:
        await process_booking_results(
            bookings.futures, web_notifier, storage, push, task_sink=push_tasks,
        )
    except Exception as exc:
        logger.exception("处理预订结果阶段失败")
        errors.append(exc)

    if push_tasks:
        try:
            results = await asyncio.gather(*push_tasks, return_exceptions=True)
            sent = sum(r for r in results if isinstance(r, int))
            errs = sum(1 for r in results if isinstance(r, Exception))
            logger.info("APNs 本轮推送结果: %d 设备成功 / %d 任务异常", sent, errs)
        except Exception:
            logger.exception("等待 push tasks 异常")
    return DeliveryResult(total_notified, errors)


def finish_round(storage, events: RoundEvents, delivery: DeliveryResult) -> None:
    """保存重试队列；阶段失败时交给管线统一告警并停止 stale 收敛。"""
    runtime.retry_queue.save(storage)
    if delivery.errors:
        raise delivery.errors[0]
    try:
        total = storage.count_all()
    except Exception:
        logger.warning("统计房源总数失败（已忽略）", exc_info=True)
        total = "未知"
    logger.info(
        "本轮结束: %d 新房源（已通知 %d），%d 状态变更，数据库共 %s 条",
        len(events.new_listings), delivery.total_notified, len(events.status_changes), total,
    )


async def _report_round_failure(exc: Exception, fresh, storage, web_notifier, prewarm) -> None:
    try:
        await prewarm.stash_pending()
    except Exception:
        logger.exception("管线失败后收集预登录结果失败")
    logger.error("抓取后管线错误（入库/通知）listings=%d: %s", len(fresh), exc, exc_info=True)
    if should_notify_internal():
        try:
            await notify_admin_only(
                storage, web_notifier,
                f"数据/通知管线错误\n\n{type(exc).__name__}: {exc}\n\n"
                f"房源已抓到 {len(fresh)} 条，但写库或发通知阶段失败"
                f"（如数据库锁死/磁盘满）。请尽快查看服务器日志与磁盘。"
                f"30 分钟内不重复通知。",
                kind="error",
            )
        except Exception:
            logger.exception("管线错误告警发送失败")


async def run_post_scrape(
    cfg, storage, user_notifiers, fresh, completeness, *, web_notifier,
    dry_run: bool, booking_deadline: float, loop, prewarm,
):
    """按顺序编排抓取后的阶段；失败时不把扫描结果用于 stale 收敛。"""
    try:
        events = prepare_round_events(cfg, storage, fresh, dry_run=dry_run)
        bookings = await submit_round_bookings(
            events, fresh, user_notifiers, storage,
            booking_deadline=booking_deadline, loop=loop, prewarm=prewarm,
        )
        from mcore import push  # 局部 import，避免冷启动加载 httpx
        delivery = await deliver_round_events(
            events, bookings, user_notifiers, storage, web_notifier, push,
        )
        finish_round(storage, events, delivery)
        return completeness
    except Exception as exc:
        await _report_round_failure(exc, fresh, storage, web_notifier, prewarm)
        return {}
