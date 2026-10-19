"""自动预订的提交与结果处理。

从 monitor.py 拆出。提交仍走 ``run_in_executor``，结果仍按用户逐个隔离；
依赖的 prewarm、H2S 抑制、重试队列与通知节流都从共享运行时状态取。
日志通道仍用 ``monitor``。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

from booker import PrewarmedSession
from models import BOOKING_HOLD_SOURCES, Listing, is_application_only
from notifier import BaseNotifier, WebNotifier
from scrapers import BlockedError
from storage import Storage
from users import UserConfig

from mcore.booking import area_key, book_with_fallback
from mcore.booking_candidates import listing_source
from mcore.h2s_guard import h2s_login_suppressed_remaining, mark_h2s_login_blocked
from mcore.notify_round import push_booking_failed
from mcore.state import runtime
from mcore.throttles import should_notify_block, should_notify_operation_rejected

logger = logging.getLogger("monitor")

# 类型别名：每个用户与其对应通知器的配对列表。
UserNotifiers = list[tuple[UserConfig, BaseNotifier]]

#: 单个用户预订结果的墙钟上限。见 ``process_booking_results``。
#:
#: ``booking_deadline`` 只决定「还要不要再试下一套备选」，它管不了一次调用
#: 本身卡死：DNS 黑洞、TCP 无响应、CF 挑战页不返回，线程会一直停在那里。
#: 而结果处理是**串行** await 的——一个用户卡住，排在他后面的所有用户都不再
#: 处理，run_once 也不返回；更糟的是默认线程池是抓取、prewarm、预订共用的，
#: 卡死线程攒几个就能把整个监控拖停。
#:
#: 与 ``SOURCE_DISPATCH_TIMEOUT_SEC`` 同一个量级：默认 600 秒足够覆盖「登录 +
#: 多套备选」的正常耗时，只在病理情况下才会触发。超时**杀不掉底层线程**
#: （executor 的 future 取消不了已在运行的调用），这里保证的是本轮继续走完，
#: 并把该用户按「本轮未预订」处理。
_BOOKING_RESULT_TIMEOUT_SEC = float(
    os.environ.get("BOOKING_RESULT_TIMEOUT_SEC") or "600")


def submit_bookings(
    loop: asyncio.AbstractEventLoop,
    ab_candidates: dict[str, list["Listing"]],
    user_notifiers: "UserNotifiers",
    prewarm_cached: dict[str, "PrewarmedSession"],
    prewarm_futures: dict[str, "asyncio.Future"],
    status_transition: dict[str, tuple[str, str]],
    booking_deadline: float,
    storage: "Storage | None" = None,
) -> list[tuple]:
    """把每个用户的候选立即提交线程池（快速下单通道）。

    H2S 保持「面积最大优先，只有 race_lost 才回退」的语义；Plaza 则每个候选
    都单独提交一次，因为 Plaza 的成功是「应征已提交」，不代表房源已被抢走。
    预订/应征请求在发出通知之前就进入对应平台（节省 1-3 秒）。

    Returns
    -------
    ab_futures: [(user, notifier, sorted_candidates, Future, prewarmed), ...]
      H2S 的 sorted_candidates 按面积降序；Plaza 每个 tuple 只包含一套房源。
    """
    ab_futures: list[tuple] = []

    for user, notifier in user_notifiers:
        candidates = ab_candidates.get(user.id, [])
        if not (user.auto_book.enabled and candidates):
            continue

        # Plaza 是申请制：一次申请成功只表示这套申请已提交，不能像 H2S 那样
        # 把成功当作「本用户本轮已经抢到房」，否则 book_with_fallback() 会在第一套
        # 成功后直接 return，后面的合格房源永远不会提交。
        plaza_candidates = sorted(
            [c for c in candidates if listing_source(c) == "plaza"],
            key=area_key,
            reverse=True,
        )
        for plaza_listing in plaza_candidates:
            if plaza_listing.id in status_transition:
                old_s, new_s = status_transition[plaza_listing.id]
                logger.info(
                    "[%s] 🚀 Plaza 快速注册 (%s → %s)，提交到 executor: %s",
                    user.name, old_s, new_s, plaza_listing.name,
                )
            else:
                logger.info(
                    "[%s] 🚀 Plaza 自动注册（全部符合条件的房源），提交到 executor: %s",
                    user.name, plaza_listing.name,
                )
            f = loop.run_in_executor(
                None,
                lambda c=plaza_listing, u=user:
                book_with_fallback([c], u, booking_deadline, prewarmed=None),
            )
            ab_futures.append((user, notifier, [plaza_listing], f, None))

        # Plaza 之外的平台仍使用原来的「主候选 + race_lost 回退」策略。这样
        # H2S、Xior、OurCampus、OurDomain 的行为都保持不变；但只有 H2S 候选会
        # 消费 prewarm、触发 403 抑制判断或复用 H2S session。
        sequential_candidates = sorted(
            [c for c in candidates if listing_source(c) != "plaza"],
            key=area_key,
            reverse=True,
        )
        if not sequential_candidates:
            continue

        h2s_candidates = [
            c for c in sequential_candidates
            if listing_source(c) == "holland2stay"
        ]
        prewarmed: PrewarmedSession | None = None
        cache_hit = False
        if h2s_candidates:
            suppressed = h2s_login_suppressed_remaining()
            if suppressed > 0:
                logger.warning(
                    "[%s] 跳过 H2S 自动预订：登录/预订 403 抑制窗口仍剩 %d 秒",
                    user.name,
                    suppressed,
                )
                continue

            # 取出该用户的预登录：优先命中缓存（同步），其次取已完成的刷新结果。
            # 未完成的 future 不 await — 让 try_book() 走正常登录 fallback，
            # 避免预登录网络延迟削弱"快速下单通道"。
            prewarmed = prewarm_cached.pop(user.id, None)
            cache_hit = prewarmed is not None
            if prewarmed is None:
                pre_fut = prewarm_futures.pop(user.id, None)
                if pre_fut is not None and pre_fut.done():
                    try:
                        prewarmed = pre_fut.result()
                    except BlockedError as e:
                        # 曾经写的是 BookingBlockedError —— 一个没人 raise 的类，
                        # 于是这条分支从未执行过：prewarm 上抛的一直是裸
                        # BlockedError，每次都落进下面的 except Exception，
                        # CF 屏蔽被静默降级成「回退正常登录」，抑制窗口形同虚设。
                        #
                        # OperationNotAllowedError 刻意不在此列（它不继承
                        # BlockedError）：那种 403 抑制多久都不会好，
                        # 落到下面返回 None、由 try_book 报 operation_rejected 才对。
                        #
                        # ``storage`` 曾经不是参数——这一行引用的是个不存在的名字，
                        # 于是**这条分支一执行就 NameError**：异常穿透 run_once，
                        # 本轮通知全丢，而抑制窗口一秒都没开。上一条注释说的「这条
                        # 分支从未执行过」正好掩盖了它：修好 BlockedError 的类型之后，
                        # 它才第一次真的跑到这里。
                        mark_h2s_login_blocked(e, storage)
                        prewarmed = None
                    except Exception:
                        prewarmed = None
                    if prewarmed:
                        runtime.prewarm_cache.set(user.id, prewarmed)
                elif pre_fut is not None:
                    # 仍在运行中，放回 futures 让 _stash_pending_prewarms 收尾
                    prewarm_futures[user.id] = pre_fut

            suppressed = h2s_login_suppressed_remaining()
            if suppressed > 0:
                logger.warning(
                    "[%s] 跳过 H2S 自动预订：prewarm 已确认 403，登录抑制窗口剩 %d 秒",
                    user.name,
                    suppressed,
                )
                continue

            if prewarmed:
                age = time.monotonic() - prewarmed.created_at
                remaining = prewarmed.token_expiry - time.monotonic()
                logger.info(
                    "[%s] ✅ 复用 prewarm（%s，已 %.0fs，剩余 %.0f 分钟）",
                    user.name, "缓存命中" if cache_hit else "新刷新",
                    age, remaining / 60,
                )
            else:
                logger.info(
                    "[%s] ⚠️  预登录未成功，下单时回退到正常登录路径",
                    user.name,
                )

        primary = sequential_candidates[0]
        if len(sequential_candidates) > 1:
            logger.info(
                "[%s] 自动预订候选 %d 套（含 %d 套备选），优先面积最大: %s (%.1f m²)",
                user.name, len(sequential_candidates), len(sequential_candidates) - 1,
                primary.name, area_key(primary),
            )
        if primary.id in status_transition:
            old_s, new_s = status_transition[primary.id]
            logger.info(
                "[%s] 🚀 快速预订通道 (%s → %s)，立即提交到 executor: %s",
                user.name, old_s, new_s, primary.name,
            )
        else:
            logger.info(
                "[%s] 🚀 自动预订（新上线可预订），立即提交到 executor: %s",
                user.name, primary.name,
            )
        f = loop.run_in_executor(
            None,
            lambda cs=sequential_candidates, u=user, pw=prewarmed:
            book_with_fallback(cs, u, booking_deadline, prewarmed=pw),
        )
        ab_futures.append((user, notifier, sequential_candidates, f, prewarmed))

    return ab_futures


async def process_booking_results(
    ab_futures: list[tuple],
    web_notifier: "WebNotifier | None",
    storage: Storage,
    push,
) -> list:
    """await 预订 Future，发送成功/失败通知，并聚合本轮屏蔽通知。

    Returns
    -------
    push_tasks: 预订失败给用户推的、屏蔽聚合时给 admin 推的 asyncio.Task（可能为空）。
    """
    push_tasks: list = []
    # 本轮被屏蔽的用户（含 notifier），所有候选 await 完后聚合发一条节流通知，
    # 避免每个用户/每个候选都发一次"预订失败"刷屏。
    blocked_in_round: list[tuple[UserConfig, BaseNotifier, str, "Listing"]] = []
    # operation 被上游拒（403 但不是 CF）。单独一个列表而不是并进 blocked_in_round：
    # 给 admin 的那条文案必须不一样——一条是"去看看 IP/指纹"，另一条是"去照抄
    # operation"。混成一条会把人引向换代理，而换代理对后者毫无用处。
    rejected_in_round: list[tuple[UserConfig, BaseNotifier, str, "Listing"]] = []

    for user, notifier, sorted_cands, future, prewarmed in ab_futures:
        # 逐个用户兜底。**没有它的话一个用户的异常会带走所有人**：这个循环负责
        # 发预订成功/失败通知、更新重试队列、聚合屏蔽通知，异常从这里穿出去，
        # 后面排队的用户就什么都没有了——而且是静默的。
        #
        # 2026-09-02 的实例：bookers/rentcafe.py 把 RentCafeSession(...) 写在
        # try 外面，构造一抛就是这个形状。根因已修，但**爆炸半径不该由被调用方
        # 来保证**。
        try:
            result = await asyncio.wait_for(
                future, timeout=_BOOKING_RESULT_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            # 底层线程可能仍卡在网络调用里；这里只解除本轮阻塞。不给该用户
            # 发通知，因为这一轮确实没有得出「订上/没订上」的结论。
            logger.error(
                "[%s] 预订任务超过 %.0f 秒没有返回，放弃本轮结果并继续处理其余用户"
                "（卡住的线程可能仍占用线程池）",
                user.name, _BOOKING_RESULT_TIMEOUT_SEC,
            )
            continue
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            logger.error(
                "[%s] 预订任务抛出未捕获异常，已隔离该用户（其余用户照常处理）",
                user.name, exc_info=True,
            )
            continue
        if result is None:
            continue
        # phase="blocked" 或 unknown_error 都意味着 session 可能已被 H2S 标记，
        # 失效 prewarm 缓存让下轮换新 session+token+TLS 指纹。
        # operation_rejected 刻意不在此列：session / token / 指纹都没问题，
        # 坏的是我们发的那条查询。丢掉缓存只会让下一轮多跑一次完整登录，
        # 然后在同一个地方以同样的方式失败。
        if prewarmed and result.phase in ("unknown_error", "blocked"):
            runtime.prewarm_cache.invalidate(user.id)
            logger.info("[%s] 因 %s 失效 prewarm 缓存", user.name, result.phase)
        # result.listing 是实际被尝试预订的那套房源（fallback 后可能不是 sorted_cands[0]）
        booked_listing = result.listing

        # 更新重试队列（dry_run 不改变队列状态，避免污染正式运行时的数据）
        if not result.dry_run:
            if result.phase == "race_lost":
                runtime.retry_queue.add(user.id, {c.id for c in sorted_cands})
                logger.info(
                    "[%s] 📝 %d 套候选加入重试队列（下次扫描仍可预订时将重试）",
                    user.name, len(sorted_cands),
                )
            elif result.phase == "blocked":
                # blocked 是 IP/指纹级，不是房源级问题；保留 runtime.retry_queue 状态不动，
                # 等下轮换指纹后再决定是否重试。
                mark_h2s_login_blocked(result.message)
                pass
            elif result.phase == "operation_rejected":
                # 同样不是房源级问题，runtime.retry_queue 也保持不动。但**不调用
                # mark_h2s_login_blocked**：那个函数假设「CF 盯上我们了，先躲
                # 一小时」，而这里 CF 什么都没做。躲一小时既救不了预订，还会把
                # 登录链路白白关掉——2026-08-19 就是这么从「预订坏了」变成
                # 「预订和登录都停了」的。
                pass
            else:
                for c in sorted_cands:
                    runtime.retry_queue.discard(user.id, c.id)

        if result.dry_run:
            logger.info("[%s] [DRY RUN] 自动预订跳过: %s", user.name, booked_listing.name)
        elif result.phase == "blocked":
            # 累积起来：admin 那条聚合成一份，用户那条按房源各发各的
            blocked_in_round.append((user, notifier, result.message, booked_listing))
        elif result.phase == "operation_rejected":
            rejected_in_round.append((user, notifier, result.message, booked_listing))
        elif result.success:
            # H2S / Xior / OurCampus / OurDomain 的成功结果表示进入占房窗口；
            # Plaza 的成功结果只表示应征已提交/已存在，平台仍要走筛选和分配流程。
            if (booked_listing.source or "").strip().lower() in BOOKING_HOLD_SOURCES:
                if storage.mark_listing_reserved_after_booking(booked_listing.id):
                    logger.info(
                        "[%s] 已将房源本地状态标记为 Reserved（booking hold）: %s",
                        user.name, booked_listing.name,
                    )
            sent = await notifier.send_booking_success(
                booked_listing, result.message, result.pay_url, result.contract_start_date
            )
            if web_notifier:
                await web_notifier.send_booking_success(
                    booked_listing,
                    result.message,
                    result.pay_url,
                    result.contract_start_date,
                    user_id=user.id,
                )
            # App 推送：预订结果原来**根本不推**——push.dispatch(kind="booked")
            # 有定义没人调。eda434e 让「只用 App」的用户也能开自动预订（推送算
            # 可达），结果他们订到了却只收到一条新房源推送，锁屏上没有「订到了、
            # 去付款」。直接 await 而不是丢进 push_tasks：下面要用它的结果判断
            # 到底有没有送达。
            try:
                pushed = await push.dispatch(storage, user, booked_listing, kind="booked")
            except Exception:
                logger.exception("[%s] 预订结果推送异常", user.name)
                pushed = 0
            # 渠道或推送任一送达就算送达。原来只看渠道：只用 App 的用户渠道列表
            # 为空、send_booking_success 恒为 False，每次成功都刷一条假 CRITICAL，
            # 真的送不到时反而被淹掉（2026-10-01 一天 5 条，全是误报）。
            delivered = bool(sent) or pushed > 0
            if not delivered and is_application_only(booked_listing):
                # 注册类没有付款时限，不需要 CRITICAL 去吵醒人——注册已经在平台上了
                logger.warning(
                    "[%s] 注册已提交但通知发送失败: %s", user.name, booked_listing.name,
                )
            elif not delivered:
                # 渠道和推送都没送达，付款链接必须保留在日志中
                # 使用 CRITICAL 级别确保即使 LOG_LEVEL=WARNING 也能被看到
                logger.critical(
                    "❌ [%s] 自动预订成功但通知发送失败，付款链接已记录于此，请立即操作：\n"
                    "  房源：%s\n"
                    "  付款：%s",
                    user.name, booked_listing.name, result.pay_url,
                )
        else:
            await notifier.send_booking_failed(booked_listing, result.message)
            if web_notifier:
                await web_notifier.send_booking_failed(
                    booked_listing,
                    result.message,
                    user_id=user.id,
                )
            # 只用 App 的用户原来收不到「没订上」：渠道为空，这里又不推。
            push_tasks.append(push_booking_failed(push, storage, user, booked_listing))

    # ── 聚合屏蔽通知（共享 scrape 的 30 min 节流，避免双重打扰）────── #
    if blocked_in_round and should_notify_block():
        names = sorted({u.name for u, _, _, _ in blocked_in_round})
        # 取第一条 msg 作为详情（所有候选的 message 通常相同 —— 都是同一 CF 屏蔽）
        detail = blocked_in_round[0][2]
        agg_msg = (
            f"🚫 自动预订被 403 屏蔽（{len(blocked_in_round)} 套候选 / "
            f"{len(names)} 个用户）\n\n{detail}\n\n"
            f"影响用户: {', '.join(names)}\n"
            f"30 分钟内不会重复通知。"
        )
        # 用户侧：走房源通知那条路，各发各的。
        #
        # 这条对用户是**该发**的——他开了自动预订，结果没订上，得知道要手动补。
        # 但上面那段聚合文案是给运维看的，直接发给用户有两个问题：技术细节
        # （403、CF、指纹）他无从处置；而且「影响用户: A, B, C」会把其他人的
        # 名字抄送给每一个人。
        for u, n, _, listing in blocked_in_round:
            await n.send_booking_failed(
                listing, "平台暂时拒绝了自动预订请求，请尽快手动预订",
            )
            push_tasks.append(push_booking_failed(push, storage, u, listing))
        if web_notifier:
            await web_notifier.send_error(agg_msg)
        # admin 也要收到 403 屏蔽通知
        push_tasks.append(asyncio.create_task(
            push.dispatch_admin(storage, agg_msg, kind="blocked"),
        ))
    elif blocked_in_round:
        logger.info(
            "🚫 %d 套候选 / %d 个用户被屏蔽，30 min 节流期内不发通知",
            len(blocked_in_round),
            len({u.id for u, _, _, _ in blocked_in_round}),
        )

    # ── operation 被上游拒（403 但不是 CF）────────────────────────── #
    # 用户侧文案与 blocked 完全一样：对用户来说结果就是「没订上，得手动补」，
    # 至于是 IP 被挡还是查询没登记，不是他能处置的信息。
    # admin 侧则必须写清楚——这条不修代码就永远不会好。
    if rejected_in_round:
        for u, n, _, listing in rejected_in_round:
            await n.send_booking_failed(
                listing, "平台暂时拒绝了自动预订请求，请尽快手动预订",
            )
            push_tasks.append(push_booking_failed(push, storage, u, listing))
        if should_notify_operation_rejected():
            names = sorted({u.name for u, _, _, _ in rejected_in_round})
            detail = rejected_in_round[0][2]
            agg_msg = (
                f"⛔ 自动预订的 GraphQL operation 未被上游放行"
                f"（{len(rejected_in_round)} 套候选 / {len(names)} 个用户）\n\n"
                f"{detail}\n\n"
                f"影响用户: {', '.join(names)}\n\n"
                "这不是 Cloudflare 屏蔽：换 IP / 换指纹 / 等冷却都无效，必须把站点"
                "自己发的那条 operation 原样照抄进 booker（见 docs/H2S.md §5.1）。"
                "在那之前每次新房上线都会以同样方式失败。\n"
                "6 小时内不会重复通知。"
            )
            if web_notifier:
                await web_notifier.send_error(agg_msg)
            push_tasks.append(asyncio.create_task(
                push.dispatch_admin(storage, agg_msg, kind="operation_rejected"),
            ))
        else:
            logger.info(
                "⛔ %d 套候选 / %d 个用户因 operation 未放行失败，"
                "6h 节流期内不发 admin 告警",
                len(rejected_in_round),
                len({u.id for u, _, _, _ in rejected_in_round}),
            )

    return push_tasks
