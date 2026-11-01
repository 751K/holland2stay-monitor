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
from types import SimpleNamespace
from dataclasses import dataclass
from typing import NamedTuple

from booker import PrewarmedSession
from models import BOOKING_HOLD_SOURCES, Listing, is_application_only
from notifier import BaseNotifier, WebNotifier
from scrapers import BlockedError
from storage import Storage
from users import UserConfig

from mcore.booking import area_key, book_with_fallback
from mcore.booking_guard import BookingGuard, attempt_context, process_alive
from mcore.plaza_guard import PlazaGuard
from mcore.booking_worker import get_booking_worker
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
#: Holland2Stay 保留账号级保护并提示用户核实，不把超时当成预订失败。
_BOOKING_RESULT_TIMEOUT_SEC = float(
    os.environ.get("BOOKING_RESULT_TIMEOUT_SEC") or "600")


def _observe_late_booking(future):
    """Consume a late worker exception after the timed-out waiter has moved on."""
    try:
        future.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("超时后的预订任务最终异常退出")


async def _notify_uncertain(storage, push, notifier, user, listing, *, listing_name=None):
    if getattr(user, "language", "zh") == "en":
        title = "Check your Holland2Stay booking"
        body = (f"The result for {listing_name or listing.name} is uncertain and the room may already "
                "be reserved. Sign in to Holland2Stay to check your orders or reservations, then "
                "confirm the review in your user settings. Auto-booking is paused for this account until then.")
    else:
        title = "Holland2Stay 预订结果需要核实"
        body = (f"{listing_name or listing.name} 的预订结果不确定，可能已经占房。请登录 Holland2Stay "
                "检查订单或预留，并在用户设置中确认核实；确认前该账号的自动预订已暂停。")
    try:
        storage.add_web_notification(type="booking", title=title, body=body,
                                     user_id=user.id, url=f"/users/{user.id}")
    except Exception:
        logger.exception("[%s] 保存预订核实通知失败", user.name)
    try:
        await notifier.send_booking_uncertain(title, body)
    except Exception:
        logger.exception("[%s] 发送预订核实通知失败", user.name)
    try:
        await push.dispatch_announcement_to_user(storage, user.id, title, body)
    except Exception:
        logger.exception("[%s] 推送预订核实通知失败", user.name)


def _plaza_uncertain_text(user, listing_name):
    if getattr(user, "language", "zh") == "en":
        title = "Check your Plaza application"
        body = (f"The application for {listing_name} may have been submitted. "
                "Sign in to Plaza to check your applications, then confirm the review in user "
                "settings. Automatic applications for this listing are paused until then.")
    else:
        title = "Plaza 应征结果需要核实"
        body = (f"{listing_name} 的应征结果不确定，可能已经提交。请登录 Plaza "
                "检查我的应征，并在用户设置中确认核实；确认前不会自动重复应征该房源。")
    return title, body


async def _notify_plaza_uncertain(storage, push, notifier, user, listing, *, listing_name=None, save_web=True):
    title, body = _plaza_uncertain_text(user, listing_name or listing.name)
    if save_web:
        try:
            storage.add_web_notification(type="booking", title=title, body=body,
                                         user_id=user.id, url=f"/users/{user.id}")
        except Exception:
            logger.exception("[%s] 保存 Plaza 核实通知失败", user.name)
    try:
        await notifier.send_booking_uncertain(title, body)
    except Exception:
        logger.exception("[%s] 发送 Plaza 核实通知失败", user.name)
    try:
        await push.dispatch_announcement_to_user(storage, user.id, title, body)
    except Exception:
        logger.exception("[%s] 推送 Plaza 核实通知失败", user.name)


async def _alert_plaza_attempt(storage, push, notifier, user, listing, guard, attempt):
    try:
        row = guard.get_attempt(attempt)
        if row and row['timed_out']:
            return
        listing_name = row['listing'] if row else listing.name
        title, body = _plaza_uncertain_text(user, listing_name)
        if row:
            if guard.claim_notice(attempt, title=title, body=body, user_id=user.id):
                await _notify_plaza_uncertain(storage, push, notifier, user, listing,
                                              listing_name=listing_name, save_web=False)
            return
    except Exception:
        logger.exception("[%s] 无法持久化 Plaza 核实通知", user.name)
    # The durable alert may be unavailable; still attempt the independent channels.
    await _notify_plaza_uncertain(storage, push, notifier, user, listing)


def notify_interrupted_plaza(storage, user_notifiers, push):
    """Warn owners after a restart even if the listing is no longer available."""
    owners = {user.id: (user, notifier) for user, notifier in user_notifiers}
    try:
        guard = PlazaGuard(storage)
        rows = guard.get_unnotified()
    except Exception:
        logger.exception("无法读取 Plaza 待核实应征")
        return
    loop = asyncio.get_running_loop()
    for row in rows:
        pair = owners.get(row['user_id'])
        if not pair or (row['state'] == 'running' and process_alive(row['pid'])):
            continue
        try:
            user, notifier = pair
            title, body = _plaza_uncertain_text(user, row['listing'])
            if guard.claim_notice(row['attempt'], title=title, body=body, user_id=user.id):
                loop.create_task(_notify_plaza_uncertain(
                    storage, push, notifier, user, SimpleNamespace(name=row['listing']), save_web=False))
        except Exception:
            logger.exception("无法恢复 Plaza 待核实通知")


class BookingJob(NamedTuple):
    """A submitted booking and the state required to process its result."""
    user: UserConfig
    notifier: BaseNotifier
    candidates: list[Listing]
    future: asyncio.Future
    prewarmed: PrewarmedSession | None


@dataclass
class BookingSubmissionContext:
    loop: asyncio.AbstractEventLoop
    prewarm_cached: dict
    prewarm_futures: dict
    status_transition: dict[str, tuple[str, str]]
    booking_deadline: float
    storage: Storage | None
    submitted: list[BookingJob]


def _submit_plaza_bookings(user, notifier, plaza_candidates, context: BookingSubmissionContext) -> None:
    """Submit each Plaza application independently, retaining durable guards."""
    from settings_store import context_job
    loop, storage = context.loop, context.storage
    status_transition, booking_deadline = context.status_transition, context.booking_deadline
    ab_futures = context.submitted
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
        plaza_guard = plaza_attempt = None
        if not user.auto_book.dry_run:
            try:
                plaza_guard = PlazaGuard(storage)
                plaza_attempt = plaza_guard.acquire(user.auto_book.plaza_username, user.id,
                                                    plaza_listing.id, plaza_listing.name)
            except Exception:
                logger.exception("[%s] 无法保存 Plaza 应征保护，已跳过该房源", user.name)
            if not plaza_attempt:
                if plaza_guard:
                    try:
                        old = plaza_guard.get_listing(user.auto_book.plaza_username, plaza_listing.id)
                        if (old and old["user_id"] == user.id and not old["timed_out"]
                                and (old["state"] == "finished" or not process_alive(old["pid"]))):
                            title, body = _plaza_uncertain_text(user, old["listing"])
                            if plaza_guard.claim_notice(old["attempt"], title=title, body=body,
                                                        user_id=user.id):
                                from mcore import push as push_module
                                loop.create_task(_notify_plaza_uncertain(
                                    storage, push_module, notifier, user, plaza_listing,
                                    listing_name=old["listing"], save_web=False))
                    except Exception:
                        logger.exception("[%s] 恢复 Plaza 核实通知失败", user.name)
                logger.warning("[%s] Plaza 应征待核实或保护不可用，跳过 %s", user.name, plaza_listing.name)
                continue

        def run_plaza(c=plaza_listing, u=user, g=plaza_guard, a=plaza_attempt):
            if g is None:
                return book_with_fallback([c], u, booking_deadline, prewarmed=None)
            result = None
            try:
                with attempt_context(g, a):
                    result = book_with_fallback([c], u, booking_deadline, prewarmed=None)
                return result
            finally:
                try:
                    g.update(a, state="finished", phase=getattr(result, "phase", "exception") or "exception")
                except Exception:
                    logger.exception("[%s] 无法保存 Plaza 应征状态，保护记录保留", u.name)

        try:
            f = loop.run_in_executor(None, context_job(run_plaza))
        except Exception:
            if plaza_guard:
                plaza_guard.update(plaza_attempt, state="finished")
                plaza_guard.clear_completed(plaza_attempt)
            raise
        if plaza_guard:
            f.plaza_guard = (plaza_guard, plaza_attempt)
        ab_futures.append(BookingJob(user, notifier, [plaza_listing], f, None))



@dataclass(frozen=True)
class PrewarmDecision:
    session: PrewarmedSession | None = None
    busy: bool = False
    suppressed: bool = False


def _prepare_h2s_prewarm(user, context: BookingSubmissionContext) -> PrewarmDecision:
    """Resolve cached or completed prelogin without waiting; retain 403 suppression."""
    suppressed = h2s_login_suppressed_remaining()
    if suppressed > 0:
        logger.warning("[%s] 跳过 H2S 自动预订：登录抑制窗口仍剩 %d 秒", user.name, suppressed)
        return PrewarmDecision(suppressed=True)
    prewarmed = context.prewarm_cached.pop(user.id, None)
    cache_hit = prewarmed is not None
    busy = False
    if prewarmed is None:
        future = context.prewarm_futures.pop(user.id, None)
        if future is not None and future.done():
            try:
                prewarmed = future.result()
            except BlockedError as exc:
                mark_h2s_login_blocked(exc, context.storage)
            except Exception:
                pass
            if prewarmed:
                runtime.prewarm_cache.set(user.id, prewarmed)
        elif future is not None:
            context.prewarm_futures[user.id] = future
            busy = True
    suppressed = h2s_login_suppressed_remaining()
    if suppressed > 0:
        logger.warning("[%s] 跳过 H2S 自动预订：prewarm 已确认 403，剩余 %d 秒", user.name, suppressed)
        return PrewarmDecision(suppressed=True)
    if prewarmed:
        age = time.monotonic() - prewarmed.created_at
        remaining = prewarmed.token_expiry - time.monotonic()
        logger.info("[%s] ✅ 复用 prewarm（%s，已 %.0fs，剩余 %.0f 分钟）",
                    user.name, "缓存命中" if cache_hit else "新刷新", age, remaining / 60)
    else:
        logger.info("[%s] ⚠️  预登录未成功，下单时回退到正常登录路径", user.name)
    return PrewarmDecision(prewarmed, busy)

def _submit_standard_bookings(user, notifier, candidates, context: BookingSubmissionContext) -> None:
    """Submit sequential booking candidates with H2S prelogin and protection."""
    from settings_store import context_job
    loop, storage = context.loop, context.storage
    status_transition, booking_deadline = context.status_transition, context.booking_deadline
    ab_futures = context.submitted
    # Plaza 之外的平台仍使用原来的「主候选 + race_lost 回退」策略。这样
    # H2S、Xior、OurCampus、OurDomain 的行为都保持不变；但只有 H2S 候选会
    # 消费 prewarm、触发 403 抑制判断或复用 H2S session。
    sequential_candidates = sorted(
        [c for c in candidates if listing_source(c) != "plaza"],
        key=area_key,
        reverse=True,
    )
    if not sequential_candidates:
        return

    h2s_candidates = [
        c for c in sequential_candidates
        if listing_source(c) == "holland2stay"
    ]
    decision = _prepare_h2s_prewarm(user, context) if h2s_candidates else PrewarmDecision()
    if decision.suppressed:
        return
    prewarmed, prewarm_busy = decision.session, decision.busy

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
    guard = attempt = None
    if h2s_candidates and not user.auto_book.dry_run:
        try:
            guard = BookingGuard(storage)
            attempt = guard.acquire(user.auto_book.email, user.id, h2s_candidates[0].name)
        except Exception:
            logger.exception("[%s] 无法保存预订保护记录，已停止 H2S 自动预订", user.name)
            attempt = None
        if not attempt:
            # Recover a warning after a monitor crash or an undelivered result.
            # Other users sharing this account must not receive the details.
            if guard:
                try:
                    old = guard.get_account(user.auto_book.email)
                    if (old and old["user_id"] == user.id and not old["timed_out"]
                            and (old["state"] == "finished" or not process_alive(old["pid"]))
                            and guard.claim_notice(old["attempt"])):
                        from mcore import push as push_module
                        loop.create_task(_notify_uncertain(storage, push_module, notifier, user, h2s_candidates[0], listing_name=old["listing"]))
                except Exception:
                    logger.exception("[%s] 恢复预订核实通知失败", user.name)
            logger.warning("[%s] H2S 账号存在待核实的预订或保护不可用，跳过自动预订", user.name)
            sequential_candidates = [c for c in sequential_candidates if listing_source(c) != "holland2stay"]
            if not sequential_candidates:
                return
            guard = None

    use_worker = any(listing_source(c) == "holland2stay" for c in sequential_candidates)
    worker = get_booking_worker(user.id + ":booking" if prewarm_busy else user.id) if use_worker else None
    job = SimpleNamespace(safe=True)

    def run_booking(cs=sequential_candidates, u=user, pw=prewarmed, g=guard, a=attempt,
                    w=worker, job_state=job):
        result = None
        try:
            if w is None:
                return book_with_fallback(cs, u, booking_deadline, prewarmed=pw)
            result = w.run(
                "book", {"candidates": cs, "user": u, "deadline": booking_deadline,
                         "guard": (g.path, a) if g else None,
                         "use_prewarm": bool(pw and getattr(pw, "worker", None) is w
                                             and pw.is_alive())},
                timeout=max(0.01, _BOOKING_RESULT_TIMEOUT_SEC - min(2.0, _BOOKING_RESULT_TIMEOUT_SEC * 0.1)),
                guard=(g, a) if g else None, job=job_state,
            )
            return result
        finally:
            if g is not None and job_state.safe:
                try:
                    g.update(a, state="finished", phase=getattr(result, "phase", "exception") or "exception",
                             pay_url="available" if getattr(result, "pay_url", "") else "")
                except Exception:
                    logger.exception("[%s] 无法保存预订最终状态，保护记录保留", u.name)

    try:
        f = loop.run_in_executor(None, context_job(run_booking))
    except Exception:
        if guard:
            guard.update(attempt, state="finished")
            guard.clear_completed(attempt)
        raise
    if guard:
        f.booking_guard = (guard, attempt)
    ab_futures.append(BookingJob(user, notifier, sequential_candidates, f, prewarmed))


def submit_bookings(
    loop: asyncio.AbstractEventLoop,
    ab_candidates: dict[str, list["Listing"]],
    user_notifiers: "UserNotifiers",
    prewarm_cached: dict[str, "PrewarmedSession"],
    prewarm_futures: dict[str, "asyncio.Future"],
    status_transition: dict[str, tuple[str, str]],
    booking_deadline: float,
    storage: "Storage | None" = None,
    *, submitted: list[BookingJob] | None = None,
) -> list[BookingJob]:
    """把每个用户的候选立即提交线程池（快速下单通道）。

    H2S 保持「面积最大优先，只有 race_lost 才回退」的语义；Plaza 则每个候选
    都单独提交一次，因为 Plaza 的成功是「应征已提交」，不代表房源已被抢走。
    预订/应征请求在发出通知之前就进入对应平台（节省 1-3 秒）。
    ``submitted`` 可由调用方提供；每次提交成功立即追加，以便后续提交出错时
    调用方仍能处理之前已发出的请求。该列表不要在执行中并发修改。

    Returns
    -------
    ab_futures: [(user, notifier, sorted_candidates, Future, prewarmed), ...]
      H2S 的 sorted_candidates 按面积降序；Plaza 每个 tuple 只包含一套房源。
    """
    ab_futures: list[BookingJob] = submitted if submitted is not None else []

    context = BookingSubmissionContext(
        loop, prewarm_cached, prewarm_futures, status_transition,
        booking_deadline, storage, ab_futures,
    )
    for user, notifier in user_notifiers:
        candidates = ab_candidates.get(user.id, [])
        if not (user.auto_book.enabled and candidates):
            continue
        plaza_candidates = sorted(
            (c for c in candidates if listing_source(c) == "plaza"),
            key=area_key, reverse=True,
        )
        _submit_plaza_bookings(user, notifier, plaza_candidates, context)
        _submit_standard_bookings(user, notifier, candidates, context)
    return ab_futures


async def _deliver_booking_success(result, user, notifier, web_notifier, storage, push, protected):
    """Persist the hold and deliver a successful result across independent channels."""
    booked_listing = result.listing
    guard, attempt = protected if protected else (None, None)
    # H2S / Xior / OurCampus / OurDomain 的成功结果表示进入占房窗口；
    # Plaza 的成功结果只表示应征已提交/已存在，平台仍要走筛选和分配流程。
    if (booked_listing.source or "").strip().lower() in BOOKING_HOLD_SOURCES:
        try:
            if storage.mark_listing_reserved_after_booking(booked_listing.id):
                logger.info(
                    "[%s] 已将房源本地状态标记为 Reserved（booking hold）: %s",
                    user.name, booked_listing.name,
                )
        except Exception:
            logger.exception("[%s] 更新预订房源本地状态失败，继续发送结果", user.name)
    try:
        sent = await notifier.send_booking_success(
            booked_listing, result.message, result.pay_url, result.contract_start_date
        )
    except Exception:
        logger.exception("[%s] 预订成功通知渠道异常", user.name)
        sent = False
    if web_notifier:
        try:
            await web_notifier.send_booking_success(
                booked_listing,
                result.message,
                result.pay_url,
                result.contract_start_date,
                user_id=user.id,
            )
        except Exception:
            logger.exception("[%s] 保存预订成功的面板通知失败", user.name)
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
    if protected and delivered:
        guard.clear_completed(attempt)
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


async def _deliver_booking_failure(result, user, notifier, web_notifier, storage, push, push_tasks):
    """Deliver an unsuccessful result without letting one channel block the others."""
    booked_listing = result.listing
    try:
        await notifier.send_booking_failed(booked_listing, result.message)
    except Exception:
        logger.exception("[%s] 预订失败通知渠道异常", user.name)
    if web_notifier:
        try:
            await web_notifier.send_booking_failed(
                booked_listing,
                result.message,
                user_id=user.id,
            )
        except Exception:
            logger.exception("[%s] 保存预订失败的面板通知失败", user.name)
    # 只用 App 的用户原来收不到「没订上」：渠道为空，这里又不推。
    push_tasks.append(push_booking_failed(push, storage, user, booked_listing))


async def _process_single_booking(
    user, notifier, sorted_cands, future, prewarmed, *, storage, web_notifier, push,
    push_tasks, blocked_in_round, rejected_in_round,
):
    """Handle one submitted result; failures cannot discard other users' results."""
    result = None
    try:
        # 逐个用户兜底。**没有它的话一个用户的异常会带走所有人**：这个循环负责
        # 发预订成功/失败通知、更新重试队列、聚合屏蔽通知，异常从这里穿出去，
        # 后面排队的用户就什么都没有了——而且是静默的。
        #
        # 2026-09-02 的实例：bookers/rentcafe.py 把 RentCafeSession(...) 写在
        # try 外面，构造一抛就是这个形状。根因已修，但**爆炸半径不该由被调用方
        # 来保证**。
        try:
            result = await asyncio.wait_for(
                asyncio.shield(future), timeout=_BOOKING_RESULT_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            future.add_done_callback(_observe_late_booking)
            logger.error("[%s] 预订任务超过等待时间，后台任务可能仍在运行", user.name)
            plaza_protected = getattr(future, "plaza_guard", None)
            protected = getattr(future, "booking_guard", None)
            if plaza_protected:
                await _alert_plaza_attempt(storage, push, notifier, user, sorted_cands[0],
                                           *plaza_protected)
            elif protected:
                guard, attempt = protected
                listing_name = None
                try:
                    guard.update(attempt, timed_out=1)
                    row = guard.get_attempt(attempt)
                    listing_name = row["listing"] if row else None
                except Exception:
                    logger.exception("[%s] 无法标记预订超时；保护记录仍会保留", user.name)
                await _notify_uncertain(storage, push, notifier, user, sorted_cands[0],
                                        listing_name=listing_name)
            return
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            if getattr(future, "booking_guard", None):
                await _notify_uncertain(storage, push, notifier, user, sorted_cands[0])
            elif getattr(future, "plaza_guard", None):
                await _alert_plaza_attempt(storage, push, notifier, user, sorted_cands[0],
                                           *future.plaza_guard)
            logger.error(
                "[%s] 预订任务抛出未捕获异常，已隔离该用户（其余用户照常处理）",
                user.name, exc_info=True,
            )
            return
        is_plaza_guard = bool(getattr(future, "plaza_guard", None))
        protected = getattr(future, "booking_guard", None) or getattr(future, "plaza_guard", None)
        if protected:
            guard, attempt = protected
            try:
                row = guard.get_attempt(attempt)
            except Exception:
                if not is_plaza_guard:
                    raise
                logger.exception("[%s] 无法读取 Plaza 应征保护，保留记录并通知用户", user.name)
                await _notify_plaza_uncertain(storage, push, notifier, user,
                                              result.listing if result else sorted_cands[0])
                return
            confirmed_plaza_rejection = (is_plaza_guard and result is not None
                                          and result.phase == "save_rejected")
            if (row and row["mutated"] and (result is None or not result.success)
                    and not confirmed_plaza_rejection):
                if is_plaza_guard:
                    await _alert_plaza_attempt(storage, push, notifier, user,
                                               result.listing if result else sorted_cands[0], guard, attempt)
                else:
                    await _notify_uncertain(storage, push, notifier, user,
                                            result.listing if result else sorted_cands[0])
                return
            if row and row["timed_out"]:
                return
            if result is None or not result.success:
                guard.clear_completed(attempt)
        if result is None:
            return
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
            await _deliver_booking_success(result, user, notifier, web_notifier, storage, push, protected)
        else:
            await _deliver_booking_failure(result, user, notifier, web_notifier, storage, push, push_tasks)

    except Exception:
        logger.exception("[%s] 处理预订结果失败，继续处理其他用户", user.name)
        if result is not None and getattr(result, "success", False):
            logger.critical("[%s] 预订成功但处理结果时发生异常，房源：%s，付款链接：%s",
                            user.name, getattr(getattr(result, "listing", None), "name", "未知"),
                            getattr(result, "pay_url", ""))
        # Existing durable guards remain in place for later verification.
        return


async def process_booking_results(
    ab_futures: list[BookingJob],
    web_notifier: "WebNotifier | None",
    storage: Storage,
    push,
    *, task_sink: list | None = None,
) -> list:
    """await 预订 Future，发送成功/失败通知，并聚合本轮屏蔽通知。

    Returns
    -------
    push_tasks: 预订失败给用户推的、屏蔽聚合时给 admin 推的 asyncio.Task（可能为空）。
    提供 task_sink 时，创建的任务立即写入该列表，异常后仍可由调用方等待。
    """
    push_tasks: list = task_sink if task_sink is not None else []
    # 本轮被屏蔽的用户（含 notifier），所有候选 await 完后聚合发一条节流通知，
    # 避免每个用户/每个候选都发一次"预订失败"刷屏。
    blocked_in_round: list[tuple[UserConfig, BaseNotifier, str, "Listing"]] = []
    # operation 被上游拒（403 但不是 CF）。单独一个列表而不是并进 blocked_in_round：
    # 给 admin 的那条文案必须不一样——一条是"去看看 IP/指纹"，另一条是"去照抄
    # operation"。混成一条会把人引向换代理，而换代理对后者毫无用处。
    rejected_in_round: list[tuple[UserConfig, BaseNotifier, str, "Listing"]] = []

    for user, notifier, sorted_cands, future, prewarmed in ab_futures:
        await _process_single_booking(
            user, notifier, sorted_cands, future, prewarmed, storage=storage,
            web_notifier=web_notifier, push=push, push_tasks=push_tasks,
            blocked_in_round=blocked_in_round, rejected_in_round=rejected_in_round,
        )

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
            try:
                await n.send_booking_failed(
                    listing, "平台暂时拒绝了自动预订请求，请尽快手动预订",
                )
            except Exception:
                logger.exception("[%s] 预订被拒通知渠道异常", u.name)
            push_tasks.append(push_booking_failed(push, storage, u, listing))
        if web_notifier:
            try:
                await web_notifier.send_error(agg_msg)
            except Exception:
                logger.exception("保存预订屏蔽告警失败")
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
            try:
                await n.send_booking_failed(
                    listing, "平台暂时拒绝了自动预订请求，请尽快手动预订",
                )
            except Exception:
                logger.exception("[%s] 预订被拒通知渠道异常", u.name)
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
                "自己发的那条 operation 原样照抄进 booker（见 docs/H2S.md §4.1）。"
                "在那之前每次新房上线都会以同样方式失败。\n"
                "6 小时内不会重复通知。"
            )
            if web_notifier:
                try:
                    await web_notifier.send_error(agg_msg)
                except Exception:
                    logger.exception("保存预订操作被拒告警失败")
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
