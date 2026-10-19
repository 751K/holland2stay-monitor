"""自动预订候选：判定、收集与多用户分配。

从 monitor.py 拆出。这一层只做内存判定，不触网、不提交订单；真正的提交与
结果处理仍留在 monitor.py（它们依赖 prewarm、H2S 抑制与通知节流）。

日志通道仍用 ``monitor``。
"""
from __future__ import annotations

import logging

from models import STATUS_AVAILABLE
from mcore.state import retry_queue

logger = logging.getLogger("monitor")

# 进这个元组的条件是「用真实账号端到端跑通过」：
#   holland2stay  2026-05-22 真实下单成功（见 ARCHITECTURE §7）
#   plaza         2026-09-10 真实应征成功（见 docs/PLAZA.md §6）
#
# Plaza 另有一道**用户侧开关** auto_book.plaza_enabled，默认关——进了这个元组
# 不等于对谁都开。理由见 can_auto_book 里的注释。
#
# **Xior 先不放进来**：bookers/rentcafe.py 里第 3 步之后的多步表单是没走过流程
# 硬猜出来的草稿，放开等于拿用户的真实账号去提交半懂不懂的表单。等流程侦察完
# 并验证过再加 "xior"。凭据判定（can_auto_book）已经就位，到时只改这个元组。
AUTO_BOOK_SOURCES: tuple[str, ...] = ("holland2stay", "plaza")


def listing_source(listing) -> str:
    """返回 listing 的规范化 source；旧数据缺 source 时按 H2S 处理。"""
    return (getattr(listing, "source", "") or "holland2stay").strip().lower()


def listing_booking_key(listing) -> tuple[str, str]:
    """自动预订去重键：同 source + id 的房源每轮只允许一个用户尝试。"""
    return listing_source(listing), str(listing.id)


def can_auto_book(user, listing) -> bool:
    """该用户能不能对这条 listing 触发自动预订。

    除了「这个 source 的预订流程实现了没有」，还要回答「用户有没有**这栋楼**的
    账号」——Xior 是一栋楼一个账号（每栋楼是独立的 RENTCafe property 门户），
    没有该楼凭据就不该产生候选。凭据本身就是开关，不需要额外的 per-building 开关。
    """
    if listing.source not in AUTO_BOOK_SOURCES:
        return False
    if listing.source == "xior":
        from scrapers.xior import building_key_for
        key = building_key_for(listing)
        if not key:
            logger.debug("Xior listing %s 无法反查楼栋，跳过自动预订", listing.id)
            return False
        email, password = user.auto_book.xior_account_for(key)
        return bool(email and password)
    if listing.source == "plaza":
        # Plaza 要**两道闸**：显式开关 + 凭据。
        #
        # 另外三个平台只用凭据当开关，因为它们都还有一步在用户手里（H2S 要付款，
        # 两个 RENTCafe 停在存草稿）。Plaza 的应征一次 POST 就落地，中间没有任何
        # 人工关卡——所以「填了凭据」不等于「授权系统替我应征」，要单独点头。
        #
        # 凭据那一半仍然重要：没凭据就不产生候选，免得每条新房源都跑一次注定失败
        # 的登录，而失败会消耗上游的尝试额度。
        ab = user.auto_book
        if not ab.plaza_enabled:
            return False
        return bool((ab.plaza_username or "").strip() and ab.plaza_password)
    return True


def can_reach_user(user, notifier, storage) -> bool:
    """能不能把「抢到了」这件事告诉用户——自动预订的前置条件。

    为什么自动预订要和「能不能通知」绑定
    ------------------------------------
    替人下了单却没法告诉他，比不下单更糟：他不知道要去付款、去确认、或者去取消。
    这条耦合是有意的，不解。

    为什么不能只看 ``notifier.has_channels``
    ----------------------------------------
    系统里有**两条**投递路径，而 ``has_channels`` 只认其中一条：

        传统渠道  create_user_notifier 从 user.notification_channels 建
                  （iMessage / Telegram / WhatsApp / 邮件）→ has_channels
        推送      mcore.push.dispatch 独立走 APNs，只看 notifications_enabled
                  与去重，**不看 has_channels**

    也就是说只用 App 的用户照常收到房源推送，却永远开不了自动预订——他配置得再
    正确也没用，而且界面上没有任何地方说过这件事。2026-09-10 线上撞到一个真实
    用户：楼盘、城市、平台、凭据、开关全对，候选阶段被这道闸挡掉，`PlazaBooker`
    一次都没被调用过。

    判据不自己写 SQL
    ----------------
    直接调 ``get_active_devices_for_user``。它的条件里有一条容易抄漏的
    ``expires_at``（``revoked`` 与「自己到期」互不蕴含），
    ``tools/backfill_push_optin.py`` 就栽在照抄 WHERE 上。判据必须是同一段代码，
    不是同一段描述。

    取不到设备时 fail-closed
    ------------------------
    查不出来 = 不确定能不能通知 = 不自动下单。这道闸存在的意义就是这个。
    """
    if notifier.has_channels:
        return True
    try:
        return bool(storage.get_active_devices_for_user(user.id))
    except Exception:
        logger.warning("查不到用户 %s 的活跃设备，本轮不产生自动预订候选",
                       user.id, exc_info=True)
        return False


def assign_auto_book_candidates(
    raw_candidates: dict[str, list],
    user_notifiers: list,
) -> dict[str, list]:
    """
    把自动预订候选从“每用户匹配”收敛成“每房源唯一归属”。

    多用户模式下，同一套房源可能同时满足多个用户的自动预订条件。直接让所有
    用户并发预订同一套房源会制造无意义的竞态，也更容易触发平台风控。这里在
    提交 executor 前做一次进程内分配：同一 listing 每轮只交给一个用户；多套
    listing 同时出现时按当前已分配数量做简单均衡，平局按用户配置顺序决定。
    """
    assigned: dict[str, list] = {u.id: [] for u, _ in user_notifiers}
    user_order = {u.id: idx for idx, (u, _) in enumerate(user_notifiers)}
    user_names = {u.id: u.name for u, _ in user_notifiers}
    assigned_count = {u.id: 0 for u, _ in user_notifiers}

    by_listing: dict[tuple[str, str], tuple[object, list[str]]] = {}
    for user, _ in user_notifiers:
        seen_for_user: set[tuple[str, str]] = set()
        for listing in raw_candidates.get(user.id, []):
            key = listing_booking_key(listing)
            if key in seen_for_user:
                continue
            seen_for_user.add(key)
            if key not in by_listing:
                by_listing[key] = (listing, [])
            by_listing[key][1].append(user.id)

    for listing, user_ids in by_listing.values():
        eligible = [uid for uid in user_ids if uid in assigned]
        if not eligible:
            continue
        chosen = min(eligible, key=lambda uid: (assigned_count[uid], user_order[uid]))
        assigned[chosen].append(listing)
        assigned_count[chosen] += 1

        if len(eligible) > 1:
            skipped = [user_names[uid] for uid in eligible if uid != chosen]
            logger.info(
                "[%s] 自动预订候选去重: %s 同时匹配 %d 个用户，已分配给当前用户，跳过: %s",
                user_names[chosen],
                listing.name,
                len(eligible),
                ", ".join(skipped),
            )

    return assigned


def collect_booking_candidates(
    new_listings: list,
    status_changes: list,
    fresh: list,
    user_notifiers: list,
    storage,
) -> tuple[dict[str, list], dict[str, tuple[str, str]]]:
    """收集每个用户的自动预订候选（不发任何通知 / 不触网）。

    「能不能通知到这个用户」每轮每人只算一次（``can_reach_user``），不在逐条房源
    的循环里查——那会变成 用户数 × 房源数 次查询。

    三个来源合并：
    1. new_listings 中新上线即 Available to book 的
    2. status_changes 中变为 Available to book 的
    3. 重试队列里上轮 race_lost、本轮仍 Available to book 的（diff() 不产事件，手动补）

    Returns
    -------
    (ab_candidates, status_transition)
      ab_candidates[user_id]   : 该用户的候选 Listing 列表（已去重 + 经分配）
      status_transition[lid]   : listing.id → (old, new)，供日志区分触发来源
    """
    ab_candidates: dict[str, list] = {u.id: [] for u, _ in user_notifiers}
    status_transition: dict[str, tuple[str, str]] = {}
    reachable: dict[str, bool] = {
        u.id: can_reach_user(u, n, storage) for u, n in user_notifiers
    }

    for listing in new_listings:
        for user, _notifier in user_notifiers:
            if (
                    user.auto_book.enabled
                    and user.notifications_enabled
                    and reachable[user.id]
                    and can_auto_book(user, listing)
                    and listing.status.lower() == STATUS_AVAILABLE
                    and (user.auto_book.listing_filter.is_empty()
                         or user.auto_book.listing_filter.passes(listing))
            ):
                ab_candidates[user.id].append(listing)

    for listing, old_status, new_status in status_changes:
        if new_status.lower() == STATUS_AVAILABLE:
            status_transition[listing.id] = (old_status, new_status)
        for user, _notifier in user_notifiers:
            if (
                    user.auto_book.enabled
                    and user.notifications_enabled
                    and reachable[user.id]
                    and can_auto_book(user, listing)
                    and new_status.lower() == STATUS_AVAILABLE
                    and (user.auto_book.listing_filter.is_empty()
                         or user.auto_book.listing_filter.passes(listing))
            ):
                ab_candidates[user.id].append(listing)

    # 重试队列检查：上次 race_lost 的候选，若仍 Available to book 则补入候选。
    # 处理"前一个预订者未付款、房子被重新放出"但状态未变的场景：
    # storage.diff() 对此类房源不产出任何事件，必须从重试队列中手动补入。
    # 这一层只按 source 粗筛（与用户无关）；「有没有这栋楼的账号」是 per-user
    # 判定，放在下面的循环里做。
    _fresh_avail = {
        l.id: l
        for l in fresh
        if l.source in AUTO_BOOK_SOURCES and l.status.lower() == STATUS_AVAILABLE
    }
    for user, _notifier in user_notifiers:
        if (not user.auto_book.enabled or not user.notifications_enabled
                or not reachable[user.id]):
            continue
        user_retry = retry_queue.get(user.id)
        if not user_retry:
            continue
        gone = user_retry - _fresh_avail.keys()
        if gone:
            retry_queue.remove_gone(user.id, gone)
            logger.info(
                "[%s] 🗑️  %d 套 race_lost 房源已不可预订，移出重试队列",
                user.name, len(gone),
            )
        existing_ids = {c.id for c in ab_candidates[user.id]}
        for lid in user_retry & _fresh_avail.keys():
            if lid in existing_ids:
                continue  # 已经由 status_changes 路径加入，跳过
            listing = _fresh_avail[lid]
            if not can_auto_book(user, listing):
                continue
            if user.auto_book.listing_filter.is_empty() or user.auto_book.listing_filter.passes(listing):
                ab_candidates[user.id].append(listing)
                logger.info(
                    "[%s] 🔁 重试 race_lost 房源（仍可预订）: %s",
                    user.name, listing.name,
                )

    ab_candidates = assign_auto_book_candidates(ab_candidates, user_notifiers)
    return ab_candidates, status_transition
