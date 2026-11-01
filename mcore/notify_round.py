"""通知阶段：未投递事件重放、影子 source 过滤、新房源/状态变更投递。

从 monitor.py 拆出。``run_once`` 只负责按顺序调用这些阶段。日志通道仍用
``monitor``——面板、告警和测试都按这个 logger 名字过滤。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import NamedTuple

from models import is_lottery_status
from storage import Storage

logger = logging.getLogger("monitor")


def merge_pending_events(
    storage: Storage,
    new_listings: list,
    status_changes: list,
) -> int:
    """把上一轮没走完通知阶段的事件并进本轮，返回补进来的条数。

    **就地修改**传入的两个列表——调用方后面还要把它们喂给通知与预订两条路，
    返回新列表会让调用点多出两个容易忘记同步的变量。

    去重按**事件主键**：本轮 ``diff()`` 刚插入的 ``status_changes`` 行也会被
    ``pending_status_changes()`` 查出来（它们同样是 notified=0），按 listing id
    去重会把「同一房源的两条不同迁移」误判成重复——而它们是两条应当分别
    投递的事件。新房源仍按 listing id 去重，那本来就是一比一的关系。
    """
    have_new = {l.id for l in new_listings}
    have_sc = {t[3] for t in status_changes}
    n = 0
    for l in storage.pending_new_listings():
        if l.id not in have_new:
            new_listings.append(l)
            have_new.add(l.id)
            n += 1
    replayed: list = []
    for tup in storage.pending_status_changes():
        if tup[3] not in have_sc:
            replayed.append(tup)
            have_sc.add(tup[3])
            n += 1
    # 待重放的事件都早于本轮，放在前面才符合时间顺序（通知文案会带新旧状态）。
    if replayed:
        status_changes[:0] = replayed
    return n


def drop_shadow_sources(
    cfg,
    new_listings: list,
    status_changes: list,
    *,
    storage: Storage | None = None,
) -> tuple[list, list]:
    """滤掉影子 source 的通知事件（房源本身已经入库，这里只拦「告诉谁」）。

    影子 source 用于新平台上线前的静默验证：照常抓取、写库、参与 stale 收敛
    和面板统计，但不发用户渠道通知、不写面板 notification feed、不推 APNs/FCM。

    ``cfg.shadow_sources`` 为空时零开销直接返回原对象。

    被丢弃的事件会**当场标记成已处理**
    ----------------------------------
    这里原本什么都不标，注释写着「副作用是这些 listing 的 notified 一直是 0，
    取消影子后不会补发历史」。那在 ``notified`` 只写不读的年代是无害的，但**加上
    未投递事件重放之后会直接翻车**：被静默拦下的房源停在 0，下一轮重放原样捞
    出来推给用户，影子 source 的整个保证当场失效。

    「丢弃」也是通知阶段的一种结论——决定了「不发」，就该记成已处理。顺带，
    原注释承诺的「解除影子不补发历史」也因此依然成立。
    """
    shadow = {s.lower() for s in getattr(cfg, "shadow_sources", None) or ()}
    if not shadow:
        return new_listings, status_changes

    def _shadowed(listing) -> bool:
        return (getattr(listing, "source", "") or "").lower() in shadow

    kept_new = [l for l in new_listings if not _shadowed(l)]
    kept_sc = [t for t in status_changes if not _shadowed(t[0])]

    if storage is not None:
        dropped_listing_ids = [l.id for l in new_listings if _shadowed(l)]
        dropped_event_ids = [t[3] for t in status_changes if _shadowed(t[0])]
        if dropped_listing_ids or dropped_event_ids:
            try:
                # 两张表的键不同：listings 按房源 id，status_changes 按事件 id。
                storage.mark_notified_batch(dropped_listing_ids)
                storage.mark_status_change_notified_batch(dropped_event_ids)
            except Exception:
                # 标记失败最多让这几条被重放一次，不该拖垮本轮通知
                logger.warning("影子 source 事件标记失败（可能被重放一次）", exc_info=True)

    dropped_new = len(new_listings) - len(kept_new)
    dropped_sc = len(status_changes) - len(kept_sc)
    if dropped_new or dropped_sc:
        logger.info(
            "🔇 影子 source %s：%d 条新房源 + %d 条状态变更已入库但不通知",
            ",".join(sorted(shadow)), dropped_new, dropped_sc,
        )
    return kept_new, kept_sc


#: 只有这个平台的抽签房源走聚合。写死而不是「所有 source」是因为判据是**平台
#: 行为**：只有 H2S 会把一批抽签房源在同一轮里一次性放出（2026-08-25 实测一轮
#: 9 套）。别的平台根本没有抽签状态，写宽了等于给一个不存在的情况留后门。
BATCH_SOURCE = "holland2stay"

#: 攒够几套才聚合。1 套时聚合与逐条完全等价，却换来一条信息更少的消息
#: （聚合版不带类型/楼层/能耗），所以从 2 起。
BATCH_MIN = 2


def split_batchable(matched: list) -> tuple[list, list]:
    """把某用户本轮的匹配拆成「聚合发」和「逐条发」两堆。

    只有 **H2S 的抽签房源**进第一堆，而且要够 ``BATCH_MIN`` 套。

    为什么只聚合抽签
    ----------------
    H2S 的抽签池不是先到先得——晚看半小时不影响抽中概率。而「可直接预订」相反，
    2026-08-25 实测中位窗口 154 分钟、最短 4 分钟，那种房源少发一条就是少一次
    机会，绝不能为了省配额把它埋进摘要里。

    闸按**平台**开而不是按状态开：OurCampus 也有抽签（Join Lottery），但它
    2026-09-15 挂出的 #2301 48 分钟就撤了，和可订一样得逐条、立刻发。

    顺序保持原样返回，不排序：调用方的日志和消息都按这个顺序输出，而
    ``new_listings`` 的顺序来自抓取，重排只会让人对不上号。
    """
    batched, singles = [], []
    for l in matched:
        if (l.source or "") == BATCH_SOURCE and is_lottery_status(l.status):
            batched.append(l)
        else:
            singles.append(l)
    if len(batched) < BATCH_MIN:
        # 不够数：原样退回逐条，且要**保持原顺序**，不能把它们甩到队尾。
        return [], matched
    return batched, singles


@dataclass
class NewListingMatches:
    """匹配阶段的结果：已遍历房源与每位用户的候选。"""
    listing_ids: list[str]
    by_user: dict[str, list]


class NewListingNotificationResult(NamedTuple):
    total_notified: int
    push_tasks: list


async def _match_new_listings(new_listings, user_notifiers, web_notifier) -> NewListingMatches:
    listing_ids: list[str] = []
    by_user: dict[str, list] = {}
    for listing in new_listings:
        for user, _ in user_notifiers:
            if not user.listing_filter.is_empty() and not user.listing_filter.passes(listing):
                logger.info("[%s] 跳过通知（过滤条件不符）: %s", user.name, listing.name)
                continue
            by_user.setdefault(user.id, []).append(listing)
        if web_notifier:
            await web_notifier.send_new_listing(listing)
        listing_ids.append(listing.id)
    return NewListingMatches(listing_ids, by_user)


async def _notify_matching_users(matches: NewListingMatches, user_notifiers) -> int:
    total = 0
    for user, notifier in user_notifiers:
        matched = matches.by_user.get(user.id) or []
        batched, singles = split_batchable(matched)
        if batched:
            logger.info("[%s] %d 套 %s 抽签房源聚合成 1 条", user.name, len(batched), BATCH_SOURCE)
            for listing in batched:
                logger.info("[%s]   ↳ %s (%s)", user.name, listing.name, listing.status)
            if await notifier.send_new_listings_batch(batched):
                total += len(batched)
        for listing in singles:
            logger.info("[%s] 新房源: %s (%s)", user.name, listing.name, listing.status)
            if await notifier.send_new_listing(listing):
                total += 1
    return total


def _schedule_new_listing_pushes(matches: NewListingMatches, user_notifiers, storage, push, tasks) -> None:
    if push.get_client() is None and push.get_fcm_client() is None:
        return
    round_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    users = {u.id: u for u, _ in user_notifiers}
    for uid, matched in matches.by_user.items():
        user = users.get(uid)
        if user is None:
            continue
        if push.should_aggregate(len(matched)):
            tasks.append(asyncio.create_task(push.dispatch_aggregate(storage, user, matched, round_id=round_id)))
        else:
            for listing in matched:
                tasks.append(asyncio.create_task(push.dispatch(storage, user, listing, kind="new")))


async def notify_new_listings(
    new_listings: list, user_notifiers: list, web_notifier, storage: Storage, push,
    *, task_sink: list | None = None,
) -> NewListingNotificationResult:
    """匹配、发送并标记已处理事件；异常时保留未完成事件供重放。

    若提供 task_sink，推送任务创建后立即加入调用方清单；即使本阶段
    后续抛错，调用方仍可等待已经排队的任务。
    """
    tasks = task_sink if task_sink is not None else []
    matches = await _match_new_listings(new_listings, user_notifiers, web_notifier)
    total = await _notify_matching_users(matches, user_notifiers)
    _schedule_new_listing_pushes(matches, user_notifiers, storage, push, tasks)
    # notified=1 表示处理过，无论是否有人匹配或渠道是否送达。
    storage.mark_notified_batch(matches.listing_ids)
    return NewListingNotificationResult(total, tasks)


async def _notify_status_event(change, user_notifiers, web_notifier, storage, push, tasks) -> int:
    listing, old_status, new_status, event_id = change
    for user, notifier in user_notifiers:
        if not user.listing_filter.is_empty() and not user.listing_filter.passes(listing):
            logger.info("[%s] 状态变更跳过通知（过滤条件不符）: %s", user.name, listing.name)
            continue
        logger.info("[%s] 状态变更: %s  %s → %s", user.name, listing.name, old_status, new_status)
        await notifier.send_status_change(listing, old_status, new_status)
        if push.get_client() is not None or push.get_fcm_client() is not None:
            tasks.append(asyncio.create_task(
                push.dispatch_status_change(storage, user, listing, old_status, new_status),
            ))
    if web_notifier:
        await web_notifier.send_status_change(listing, old_status, new_status)
    return event_id


async def notify_status_changes(
    status_changes: list, user_notifiers: list, web_notifier, storage: Storage, push,
    *, task_sink: list | None = None,
) -> list:
    """逐事件发送并按事件主键标记；task_sink 保存异常前已排队的推送。"""
    tasks = task_sink if task_sink is not None else []
    ids = []
    for change in status_changes:
        ids.append(await _notify_status_event(change, user_notifiers, web_notifier, storage, push, tasks))
    storage.mark_status_change_notified_batch(ids)
    return tasks


def push_booking_failed(push, storage, user, listing) -> "asyncio.Task":
    """给用户推一条「没订上」。丢进 push_tasks，run_once 末尾统一 gather。

    不像成功那条要 await：失败的送达与否不改变任何判断。push.dispatch 自己吞
    异常、返回 0，不会让 gather 炸。
    """
    return asyncio.create_task(
        push.dispatch(storage, user, listing, kind="booking_failed"),
    )
