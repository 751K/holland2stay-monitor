"""通知阶段：未投递事件重放、影子 source 过滤、新房源/状态变更投递。

从 monitor.py 拆出。``run_once`` 只负责按顺序调用这些阶段。日志通道仍用
``monitor``——面板、告警和测试都按这个 logger 名字过滤。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

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

    去重按 listing id：本轮 diff 已经产出的不再补一遍（同一条房源刚上架又立刻
    变状态时会撞上）。
    """
    have_new = {l.id for l in new_listings}
    have_sc = {t[0].id for t in status_changes}
    n = 0
    for l in storage.pending_new_listings():
        if l.id not in have_new:
            new_listings.append(l)
            have_new.add(l.id)
            n += 1
    for tup in storage.pending_status_changes():
        if tup[0].id not in have_sc:
            status_changes.append(tup)
            have_sc.add(tup[0].id)
            n += 1
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
        dropped_ids = (
            [l.id for l in new_listings if _shadowed(l)]
            + [t[0].id for t in status_changes if _shadowed(t[0])]
        )
        if dropped_ids:
            try:
                storage.mark_notified_batch(dropped_ids)
                storage.mark_status_change_notified_batch(dropped_ids)
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


async def notify_new_listings(
    new_listings: list,
    user_notifiers: list,
    web_notifier,
    storage: Storage,
    push,
) -> tuple[int, list]:
    """发送新房源通知（预订已在后台线程并行运行）。

    标记策略：**走完本阶段的 listing 全部标记**，与「有没有人匹配」「投递成功
    与否」都无关。``notified=1`` 的语义是「已处理」，不是「已送达」。

    这个区别以前无所谓（没人读这个字段），现在它是未投递事件重放的判据——
    旧写法（只在至少投递成功时标记）会让无人匹配的房源永远停在 0，生产实测
    559 条里有 403 条是这么来的，重放信号会被这批噪音淹没。

    Returns
    -------
    (total_notified, push_tasks)
      push_tasks: 本段创建的 APNs/FCM asyncio.Task，由 run_once 末尾统一 gather。
    """
    total_notified = 0
    new_notified_ids: list[str] = []
    user_round_matches: dict[str, list] = {}  # user_id -> [Listing,...]，用于聚合判定
    push_tasks: list = []

    for listing in new_listings:
        for user, _notifier in user_notifiers:
            if not user.listing_filter.is_empty() and not user.listing_filter.passes(listing):
                logger.info("[%s] 跳过通知（过滤条件不符）: %s", user.name, listing.name)
                continue

            # APNs 推送钩子：现有渠道发送之后追加，独立 task，与其他渠道互不阻塞。
            user_round_matches.setdefault(user.id, []).append(listing)

        # Web 面板通知（每条新房源写一次，与用户过滤无关）
        if web_notifier:
            await web_notifier.send_new_listing(listing)

        # **走完通知阶段就标记，不管有没有人匹配。**
        #
        # 旧写法是 `if notified_this`（至少投递给一个用户）。于是没有任何用户
        # 筛选条件匹配到的房源永远停在 notified=0——2026-08-20 生产实测，559 条
        # listings 里有 403 条是这么来的。
        #
        # 这个语义差别以前无所谓（没人读这个字段），现在它是重放的判据：不修的话
        # 0 池会立刻重新堆积，重放信号退化成噪音。见
        # mstorage/_listings.py 的 pending_new_listings。
        new_notified_ids.append(listing.id)

    # 逐条 / 聚合发送。
    #
    # 这一段从上面的 `for listing: for user:` 里拆了出来——聚合必须先把某个用户
    # 本轮的匹配收齐才能判断，边遍历边发做不到。代价是日志里「新房源」那几行
    # 现在按用户分组，不再按房源分组。
    for user, notifier in user_notifiers:
        matched = user_round_matches.get(user.id) or []
        batched, singles = split_batchable(matched)
        if batched:
            logger.info(
                "[%s] %d 套 %s 抽签房源聚合成 1 条（逐条发会占掉当天邮件配额的一大半）",
                user.name, len(batched), BATCH_SOURCE,
            )
            for l in batched:
                logger.info("[%s]   ↳ %s (%s)", user.name, l.name, l.status)
            if await notifier.send_new_listings_batch(batched):
                # 计的是「这些房源都通知到了这个用户」，语义和拆分前一致；
                # 实际发出的消息数是 1，那件事由上面那行日志表达。
                total_notified += len(batched)
        for listing in singles:
            logger.info("[%s] 新房源: %s (%s)", user.name, listing.name, listing.status)
            if await notifier.send_new_listing(listing):
                total_notified += 1

    # APNs + FCM 发送：本轮每个用户的匹配若 < 阈值，按条推；否则聚合成一条
    if push.get_client() is not None or push.get_fcm_client() is not None:
        round_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        user_by_id = {u.id: u for u, _ in user_notifiers}
        for uid, matched in user_round_matches.items():
            user_obj = user_by_id.get(uid)
            if user_obj is None:
                continue
            if push.should_aggregate(len(matched)):
                push_tasks.append(asyncio.create_task(
                    push.dispatch_aggregate(
                        storage, user_obj, matched, round_id=round_id,
                    ),
                ))
            else:
                for l in matched:
                    push_tasks.append(asyncio.create_task(
                        push.dispatch(storage, user_obj, l, kind="new"),
                    ))

    storage.mark_notified_batch(new_notified_ids)
    return total_notified, push_tasks


async def notify_status_changes(
    status_changes: list,
    user_notifiers: list,
    web_notifier,
    storage: Storage,
    push,
) -> list:
    """发送状态变更通知（预订已在后台线程并行运行）。

    Returns
    -------
    push_tasks: 本段创建的 APNs/FCM asyncio.Task。
    """
    push_tasks: list = []
    sc_notified_ids: list[str] = []

    for listing, old_status, new_status in status_changes:
        for user, notifier in user_notifiers:
            if not user.listing_filter.is_empty() and not user.listing_filter.passes(listing):
                logger.info("[%s] 状态变更跳过通知（过滤条件不符）: %s", user.name, listing.name)
                continue

            logger.info("[%s] 状态变更: %s  %s → %s", user.name, listing.name, old_status, new_status)
            await notifier.send_status_change(listing, old_status, new_status)
            # APNs + FCM status_change：直接逐条推（变更通常不像新房源那么密集）
            if push.get_client() is not None or push.get_fcm_client() is not None:
                push_tasks.append(asyncio.create_task(
                    push.dispatch_status_change(
                        storage, user, listing, old_status, new_status,
                    ),
                ))

        # Web 面板通知（每次状态变更写一次，与用户过滤无关）
        if web_notifier:
            await web_notifier.send_status_change(listing, old_status, new_status)

        # 同上：走完通知阶段就标记，判据是「处理过」不是「投递成功」。
        sc_notified_ids.append(listing.id)

    storage.mark_status_change_notified_batch(sc_notified_ids)
    return push_tasks


def push_booking_failed(push, storage, user, listing) -> "asyncio.Task":
    """给用户推一条「没订上」。丢进 push_tasks，run_once 末尾统一 gather。

    不像成功那条要 await：失败的送达与否不改变任何判断。push.dispatch 自己吞
    异常、返回 0，不会让 gather 炸。
    """
    return asyncio.create_task(
        push.dispatch(storage, user, listing, kind="booking_failed"),
    )
