"""
monitor.py — 监控主程序
========================
程序入口，协调抓取、存储、通知、自动预订的完整流程。

运行方式
--------
    python monitor.py           持续监控（默认，带智能轮询和 SIGHUP 热重载）
    python monitor.py --once    单次运行后退出（适合 cron 任务）
    python monitor.py --test    抓取并打印 JSON，不写库不发通知（用于验证抓取）

核心流程（每轮）
----------------
1. `scrape_all()`（sync，在 executor 线程中运行）抓取所有城市房源
2. `storage.diff()` 对比库中快照，产出 new_listings / status_changes
3. 遍历启用的用户，构建自动预订候选
4. 预登录（create_prewarmed_session）：为有候选的用户提前换取 Bearer token
   （与步骤 5 的通知发送并发进行）
5. 发送新房源/状态变更通知
6. 汇集预登录结果，以预认证 Session 提交 try_book() 到线程池
7. 等待预订完成，推送预订结果通知，清理预登录 Session
8. 写 meta（last_scrape_at）；每 HEARTBEAT_EVERY 轮发心跳

智能轮询
--------
_get_interval() 根据荷兰本地时间判断是否处于高峰期（默认 8:30-10:00 工作日）。
高峰期使用 PEAK_INTERVAL（默认 60s），其余时间使用 CHECK_INTERVAL（默认 300s）。
实际等待时间在基准值 ±20% 随机抖动，避免多实例同步。

热重载
------
收到 SIGHUP 信号后，在本轮结束时重载 .env + users.json，无需重启进程。
Web 面板的「立即应用」按钮通过发送 SIGHUP（`kill -HUP <PID>`）触发。

依赖模块
--------
scraper → storage → notifier → booker（单向，无循环）
config / users：被各模块按需 import
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from booker import create_prewarmed_session, try_book
from config import DATA_DIR, ENV_PATH, load_config
from models import STATUS_AVAILABLE, parse_float
from notifier import BaseNotifier, WebNotifier, create_user_notifier
from scraper import RateLimitError, scrape_all
from storage import Storage
from users import USERS_FILE, UserConfig, load_users, migrate_from_env, save_users


def _setup_logging(level: str) -> None:
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(level=getattr(logging, level, "INFO"), format=fmt)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


logger = logging.getLogger("monitor")

HEARTBEAT_EVERY = 12   # 默认 5min * 12 = 1h

# 自适应轮询参数（固定，不需要用户配置）
# 每轮成功后将当前间隔乘以此系数（5% 缩短），缓慢逼近 min_interval
_ADAPTIVE_DECREASE = 0.95
# 遭遇 429 后将当前间隔乘以此系数（翻倍），快速退避
_ADAPTIVE_INCREASE = 2.0

_PID_FILE = DATA_DIR / "monitor.pid"
_RELOAD_REQUEST_FILE = DATA_DIR / "monitor.reload"
_AMS = ZoneInfo("Europe/Amsterdam")

# 热重载事件（SIGHUP → 唤醒 main_loop 中的 sleep，立即重载配置）
_reload_event: asyncio.Event | None = None

# 竞争失败重试队列：user_id → {listing_id, ...}
#
# 背景
# ----
# storage.diff() 只产出"新增"和"状态变更"两类事件。如果一套房子在上轮就是
# "Available to book" 且状态未变（如前一个预订者未付款、房子被重新放出），
# 它既不进 new_listings 也不进 status_changes，自动预订永远不会重试。
#
# 解决方案
# --------
# try_book 竞争失败（race_lost）时，将候选 listing_id 加入此队列。
# 每轮 run_once 开始时，检查队列中的 ID 是否仍在本次抓取的 "Available to book"
# 列表中，若是则直接加入 ab_candidates，触发新一轮预订尝试。
#
# 持久化
# ------
# 队列通过 Storage.save_retry_queue() 落盘到 SQLite meta 表，
# 进程重启后由 _async_main() 恢复，确保不会因重启错过重试窗口。
_retry_queue: dict[str, set[str]] = {}
_retry_queue_dirty = False

# 类型别名：每个用户与其对应通知器的配对列表
UserNotifiers = list[tuple[UserConfig, BaseNotifier]]


# ------------------------------------------------------------------ #
# PID & 信号管理
# ------------------------------------------------------------------ #

def _write_pid() -> None:
    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(str(os.getpid()))
    logger.debug("PID %d 已写入 %s", os.getpid(), _PID_FILE)


def _remove_pid() -> None:
    _PID_FILE.unlink(missing_ok=True)


def _consume_reload_request_file() -> bool:
    """
    消费一次文件触发的热重载请求。

    Returns
    -------
    True  : 检测到请求并已删除请求文件
    False : 当前没有待处理请求

    说明
    ----
    这是 Windows 上 Web 面板「立即生效」的主要通信方式。
    在 Unix 上也作为 SIGHUP 失败时的回退方案。
    """
    if not _RELOAD_REQUEST_FILE.exists():
        return False
    try:
        _RELOAD_REQUEST_FILE.unlink()
    except FileNotFoundError:
        return False
    return True


def _setup_signals(loop: asyncio.AbstractEventLoop) -> None:
    """注册 SIGHUP 处理器：收到信号后唤醒热重载事件。"""
    def _handler(signum: int, frame) -> None:  # type: ignore[type-arg]
        if _reload_event is not None:
            loop.call_soon_threadsafe(_reload_event.set)
            logger.info("收到 SIGHUP，将在本轮结束后热重载配置")

    try:
        signal.signal(signal.SIGHUP, _handler)
    except (OSError, AttributeError):
        logger.debug("SIGHUP 不可用（非 Unix 系统），跳过信号注册")


# ------------------------------------------------------------------ #
# 智能轮询
# ------------------------------------------------------------------ #

def _get_interval(cfg) -> tuple[int, bool]:
    """
    根据荷兰本地时间（Europe/Amsterdam）判断当前是否处于高峰期。

    高峰期判断逻辑
    --------------
    1. 若 cfg.peak_weekdays_only=True 且当天是周末 → 非高峰
    2. 解析 cfg.peak_start / cfg.peak_end（HH:MM 格式）
    3. 当前分钟数落在 [start, end] 区间内 → 高峰

    Returns
    -------
    (interval_seconds, is_peak)
    interval_seconds : 本轮应等待的基准秒数（抖动前）
    is_peak          : True 表示当前处于高峰期
    """
    now = datetime.now(_AMS)
    if cfg.peak_weekdays_only and now.weekday() >= 5:
        return cfg.check_interval, False
    try:
        sh, sm = map(int, cfg.peak_start.split(":"))
        eh, em = map(int, cfg.peak_end.split(":"))
    except ValueError:
        return cfg.check_interval, False
    cur = now.hour * 60 + now.minute
    if sh * 60 + sm <= cur <= eh * 60 + em:
        return cfg.peak_interval, True
    return cfg.check_interval, False


def _apply_jitter(seconds: int, ratio: float = 0.20) -> int:
    """
    在基准等待时间上叠加随机抖动，避免多实例在同一秒发起请求。

    Parameters
    ----------
    seconds : 基准等待时间（秒）
    ratio   : 抖动比例（0–0.5），来自 cfg.jitter_ratio；
              e.g. 0.20 → 实际时间在 [seconds*0.8, seconds*1.2] 内随机

    Returns
    -------
    实际等待时间（秒），最小 5 秒
    """
    delta = seconds * ratio
    return max(5, int(seconds + random.uniform(-delta, delta)))


# ------------------------------------------------------------------ #
# 核心逻辑
# ------------------------------------------------------------------ #

def _area_key(listing) -> float:
    """
    从 Listing.feature_map() 提取面积数值，用于多套候选时按面积降序选最大。

    Returns
    -------
    float 面积值（m²）；无法解析时返回 0.0（排在最后）
    """
    area_str = listing.feature_map().get("area", "")
    val = parse_float(area_str)
    return val if val is not None else 0.0


def _book_with_fallback(
    sorted_candidates: list,
    user: "UserConfig",
    deadline: float,
    prewarmed: "PrewarmedSession | None" = None,
) -> "BookingResult":
    """
    按面积降序依次对 sorted_candidates 中的房源尝试 try_book()。

    重试条件
    --------
    仅在 result.phase == "race_lost"（房源已被他人抢先预订）时继续尝试下一套。
    其余失败类型（reserved_conflict / unknown_error 等）立即返回——这些错误
    与具体房源无关，换一套也无法解决。

    截止时间
    --------
    第一套无条件尝试，确保用户不会因截止超时而错过所有机会。
    从第二套起，仅在 deadline 之前继续，避免占用下一轮扫描的时间窗口。
    deadline = float('inf') 表示无限制（--once 模式或单轮模式）。

    Parameters
    ----------
    sorted_candidates : 已按面积降序排列的候选房源列表（调用方保证非空）
    user              : 用户配置（含预订账号、密码、支付方式等）
    deadline          : time.monotonic() 截止时刻；超过则停止对下一套的尝试
    prewarmed         : 预认证 Session，传入时跳过 try_book() 中的登录步骤

    Returns
    -------
    最后一次 try_book() 的 BookingResult（成功或最终失败）
    """
    last_result = None
    for i, listing in enumerate(sorted_candidates):
        # 第一套无条件尝试；备选套先检查截止时间
        if i > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning(
                    "[%s] ⏰ 已到下次扫描截止，停止备选重试"
                    "（已尝试 %d/%d，剩余 %d 套未试）",
                    user.name, i, len(sorted_candidates), len(sorted_candidates) - i,
                )
                break
            logger.info(
                "[%s] 🔄 竞争失败，尝试备选 %d/%d: %s (%.1f m²)"
                "，距截止还剩 %.0f 秒",
                user.name, i + 1, len(sorted_candidates),
                listing.name, _area_key(listing), remaining,
            )

        result = try_book(
            listing,
            user.auto_book.email,
            user.auto_book.password,
            dry_run=user.auto_book.dry_run,
            cancel_enabled=user.auto_book.cancel_enabled,
            payment_method=user.auto_book.payment_method,
            prewarmed=prewarmed,
        )
        last_result = result

        if result.success or result.dry_run or result.phase != "race_lost":
            return result
        # race_lost → 继续下一套

    # 所有候选均已尝试（均 race_lost）或截止时间到
    return last_result  # type: ignore[return-value]  # sorted_candidates 非空保证非 None


async def run_once(
    cfg,
    storage: Storage,
    user_notifiers: UserNotifiers,
    *,
    web_notifier: WebNotifier | None = None,
    dry_run: bool = False,
    booking_deadline: float = float("inf"),
) -> None:
    """
    执行一次完整的「抓取 → 对比 → 通知 → 自动预订」流程。

    Parameters
    ----------
    cfg              : 当前全局配置（Config 实例）
    storage          : SQLite 持久化层
    user_notifiers   : [(UserConfig, BaseNotifier), ...]，启用的用户列表
    dry_run          : True 时（--test 模式）只打印结果，不写库不发通知
    booking_deadline : time.monotonic() 截止时刻，传给 _book_with_fallback()；
                       超过截止时不再尝试备选房源。
                       默认 float("inf") = 无限制（--once / --test 模式）。

    流程说明
    --------
    1. scrape_all() 在 executor 线程中运行（同步 → 异步桥接）
    2. storage.diff() 识别 new_listings 和 status_changes
    3. 快速候选预扫描（纯内存，无网络）：
       - 同时扫描 new_listings 和 status_changes，收集每个用户的自动预订候选
       - 记录"状态变更 → Available to book"（快速通道），与普通新上线房源加以区分
       - 立即将 try_book() 提交到线程池（run_in_executor），预订与通知并行执行
    4. 遍历 new_listings：发送新房源通知（预订已在后台运行）
    5. 遍历 status_changes：发送状态变更通知（预订已在后台运行）
    6. await 预订 Future，发送预订成功/失败通知
    7. 更新 meta（last_scrape_at / last_scrape_count）

    并行策略
    --------
    try_book() 是同步函数，通过 run_in_executor 在线程池中运行。
    它在步骤 3 末尾立即提交，步骤 4/5 的通知网络调用（send_*）与之并行进行。
    到步骤 6 await 时，booking 往往已经完成，几乎零额外等待。

    对于"Reserved / Not available → Available to book"这类高竞争变更，
    预订请求会在发出状态变更通知之前就已进入 Holland2Stay 服务器，
    相比原先"通知发完再预订"的顺序执行，可节省 1-3 秒。
    """
    global _retry_queue_dirty
    city_tasks, availability_ids = cfg.scrape_tasks()
    logger.info("开始抓取，城市数: %d，活跃用户数: %d", len(city_tasks), len(user_notifiers))

    if not city_tasks:
        logger.warning("未配置任何目标城市（CITIES 为空），本轮不抓取。请检查 .env 中 CITIES 设置。")
        return

    try:
        fresh = await asyncio.get_running_loop().run_in_executor(
            None, lambda: scrape_all(city_tasks, availability_ids)
        )
    except RateLimitError as e:
        # 429 需要更长冷却，上传给 main_loop 单独处理（不走普通 10s 恢复路径）
        logger.warning("⚠️  抓取被限流: %s", e)
        if not dry_run:
            err_msg = f"⚠️ 抓取被限流（429）\n{e}\n监控将暂停 5 分钟后继续。"
            for _, notifier in user_notifiers:
                await notifier.send_error(err_msg)
            if web_notifier:
                await web_notifier.send_error(err_msg)
        raise
    except Exception as e:
        logger.error("抓取全部失败: %s", e, exc_info=True)
        if not dry_run:
            err_msg = f"抓取失败: {e}"
            for _, notifier in user_notifiers:
                await notifier.send_error(err_msg)
            if web_notifier:
                await web_notifier.send_error(err_msg)
        return

    logger.info("本次抓取共 %d 条房源", len(fresh))

    if dry_run:
        print(f"\n{'='*60}")
        print(f"[DRY RUN] 抓取结果（共 {len(fresh)} 条）")
        for user, _ in user_notifiers:
            if not user.listing_filter.is_empty():
                matched = [l for l in fresh if user.listing_filter.passes(l)]
                print(f"  用户 [{user.name}] 过滤后符合：{len(matched)} 条")
        print('='*60)
        for l in fresh:
            print(f"  [{l.status:22s}] {l.price_display:7s} | {l.available_from or '?':12s} | {l.name}")
        print('='*60)
        return

    new_listings, status_changes = storage.diff(fresh)

    # diff() 成功后再写时间戳，确保面板显示的 last_scrape_at 对应一次完整的
    # "抓取 + 入库" 操作；若 diff() 抛异常，时间戳不会被更新。
    storage.set_meta("last_scrape_at", datetime.now(timezone.utc).isoformat())
    storage.set_meta("last_scrape_count", str(len(fresh)))

    # ── 快速候选预扫描：立即收集候选，抢在发通知之前提交预订 ──────── #
    # 此处只做过滤判断（纯内存），不发任何通知
    ab_candidates: dict[str, list] = {u.id: [] for u, _ in user_notifiers}

    # listing.id → (old_status, new_status)：记录状态变更触发的候选，用于日志区分
    status_transition: dict[str, tuple[str, str]] = {}

    for listing in new_listings:
        for user, _ in user_notifiers:
            if (
                user.auto_book.enabled
                and listing.status.lower() == STATUS_AVAILABLE
                and (user.auto_book.listing_filter.is_empty()
                     or user.auto_book.listing_filter.passes(listing))
            ):
                ab_candidates[user.id].append(listing)

    for listing, old_status, new_status in status_changes:
        if new_status.lower() == STATUS_AVAILABLE:
            status_transition[listing.id] = (old_status, new_status)
        for user, _ in user_notifiers:
            if (
                user.auto_book.enabled
                and new_status.lower() == STATUS_AVAILABLE
                and (user.auto_book.listing_filter.is_empty()
                     or user.auto_book.listing_filter.passes(listing))
            ):
                ab_candidates[user.id].append(listing)

    # ── 重试队列检查：上次 race_lost 的候选，若仍 Available to book 则补入候选 ── #
    # 这处理了"前一个预订者未付款、房子被重新放出"但状态未变的场景：
    # storage.diff() 对此类房源不产出任何事件，必须从重试队列中手动补入。
    _fresh_avail = {l.id: l for l in fresh if l.status.lower() == STATUS_AVAILABLE}
    for user, _ in user_notifiers:
        if not user.auto_book.enabled:
            continue
        user_retry = _retry_queue.get(user.id)
        if not user_retry:
            continue
        # 清理本次抓取中已不再可预订的房源（已被成功预订 / 状态变更为其他）
        gone = user_retry - _fresh_avail.keys()
        if gone:
            user_retry -= gone          # set 原地修改，直接影响 _retry_queue[user.id]
            _retry_queue_dirty = True
            logger.info(
                "[%s] 🗑️  %d 套 race_lost 房源已不可预订，移出重试队列",
                user.name, len(gone),
            )
        # 将仍可预订的重试房源加入候选（避免与 status_changes 路径重复）
        existing_ids = {c.id for c in ab_candidates[user.id]}
        for lid in user_retry & _fresh_avail.keys():
            if lid in existing_ids:
                continue  # 已经由 status_changes 路径加入，跳过
            listing = _fresh_avail[lid]
            if user.auto_book.listing_filter.is_empty() or user.auto_book.listing_filter.passes(listing):
                ab_candidates[user.id].append(listing)
                logger.info(
                    "[%s] 🔁 重试 race_lost 房源（仍可预订）: %s",
                    user.name, listing.name,
                )

    # ── 预登录：为有候选房源的用户提前创建已认证 Session ────────── #
    # 提交到 executor 后立即进入通知循环，预登录与通知的 HTTP 调用
    # 并发进行（不同线程），在 await 预订结果之前汇集即可。
    loop = asyncio.get_running_loop()
    prewarm_futures: dict[str, asyncio.Future] = {}
    for user, _ in user_notifiers:
        if not (user.auto_book.enabled and ab_candidates.get(user.id)):
            continue
        prewarm_futures[user.id] = loop.run_in_executor(
            None,
            lambda u=user: create_prewarmed_session(u.auto_book.email, u.auto_book.password),
        )
        logger.debug("[%s] 预登录已提交到 executor", user.name)

    # 立即将 _book_with_fallback() 提交到线程池，返回 Future（非阻塞）
    # 注意：预订 Future 与预登录 Future 共享同一个默认线程池。
    # 若预登录尚未完成，booking 线程会在 try_book() 中按正常路径登录
    # （prewarmed=None），不会等待。
    # ab_futures: list of (user, notifier, sorted_candidates, Future[BookingResult])
    # sorted_candidates 按面积降序排列；fallback 逻辑在线程内部按序尝试
    ab_pending: list[tuple] = []

    for user, notifier in user_notifiers:
        candidates = ab_candidates.get(user.id, [])
        if not (user.auto_book.enabled and candidates):
            continue
        sorted_cands = sorted(candidates, key=_area_key, reverse=True)
        primary = sorted_cands[0]
        if len(sorted_cands) > 1:
            logger.info(
                "[%s] 自动预订候选 %d 套（含 %d 套备选），优先面积最大: %s (%.1f m²)",
                user.name, len(sorted_cands), len(sorted_cands) - 1,
                primary.name, _area_key(primary),
            )
        # 区分：状态变更触发（快速通道）vs 新上线房源
        if primary.id in status_transition:
            old_s, new_s = status_transition[primary.id]
            logger.info(
                "[%s] 🚀 快速预订通道 (%s → %s)，立即提交: %s",
                user.name, old_s, new_s, primary.name,
            )
        else:
            logger.info("[%s] 🔖 自动预订（新上线可预订），立即提交: %s", user.name, primary.name)

        # 暂存候选数据，预登录完成后统一提交到 executor
        # （预登录与下方通知循环并发进行，让 login 往返在通知发送期间完成）
        ab_pending.append((user, notifier, sorted_cands))

    # ── 新房源通知（预登录与通知并发进行）────────────────────────── #
    total_notified = 0
    new_notified_ids: list[str] = []
    for listing in new_listings:
        notified_this = False
        for user, notifier in user_notifiers:
            if not user.listing_filter.is_empty() and not user.listing_filter.passes(listing):
                logger.info("[%s] 跳过通知（过滤条件不符）: %s", user.name, listing.name)
                continue

            logger.info("[%s] 新房源: %s (%s)", user.name, listing.name, listing.status)
            ok = await notifier.send_new_listing(listing)
            if ok:
                notified_this = True
                total_notified += 1

        # Web 面板通知（每条新房源写一次，与用户过滤无关）
        if web_notifier:
            await web_notifier.send_new_listing(listing)

        if notified_this:
            new_notified_ids.append(listing.id)

    storage.mark_notified_batch(new_notified_ids)

    # ── 状态变更通知（预登录与通知并发进行）──────────────────────── #
    sc_notified_ids: list[str] = []
    for listing, old_status, new_status in status_changes:
        notified_this = False
        for user, notifier in user_notifiers:
            if not user.listing_filter.is_empty() and not user.listing_filter.passes(listing):
                logger.info("[%s] 状态变更跳过通知（过滤条件不符）: %s", user.name, listing.name)
                continue

            logger.info("[%s] 状态变更: %s  %s → %s", user.name, listing.name, old_status, new_status)
            ok = await notifier.send_status_change(listing, old_status, new_status)
            if ok:
                notified_this = True

        # Web 面板通知（每次状态变更写一次，与用户过滤无关）
        if web_notifier:
            await web_notifier.send_status_change(listing, old_status, new_status)

        if notified_this:
            sc_notified_ids.append(listing.id)

    storage.mark_status_change_notified_batch(sc_notified_ids)

    # ── 汇集预登录结果，提交预订 Future ──────────────────────────── #
    # 到此处通知已全部发出（通常耗时 1-3s），预登录的 executor 任务
    # 应已完成。若预登录失败（网络/凭据错误），记录警告并回退到
    # try_book() 的内部登录路径（prewarmed=None），不阻塞预订。
    prewarmed_sessions: dict[str, "PrewarmedSession"] = {}
    for uid, f in prewarm_futures.items():
        try:
            prewarmed_sessions[uid] = await f
            logger.debug("%s 预登录完成，session 可供复用", uid)
        except Exception as e:
            logger.warning(
                "%s 预登录失败，try_book 将回退到正常登录路径: %s",
                uid, e,
            )

    ab_futures: list[tuple] = []
    for user, notifier, sorted_cands in ab_pending:
        ps = prewarmed_sessions.get(user.id)
        if ps:
            logger.info(
                "[%s] 🔓 使用预登录 session (email=%s)",
                user.name, ps.email,
            )

        future = loop.run_in_executor(
            None,
            lambda cs=sorted_cands, u=user, dl=booking_deadline, p=ps:
                _book_with_fallback(cs, u, dl, prewarmed=p),
        )
        ab_futures.append((user, notifier, sorted_cands, future))

    # ── 等待预订结果，发送成功/失败通知 ──────────────────────────── #
    for user, notifier, sorted_cands, future in ab_futures:
        result = await future
        # result.listing 是实际被尝试预订的那套房源（fallback 后可能不是 sorted_cands[0]）
        booked_listing = result.listing

        # 更新重试队列（dry_run 不改变队列状态，避免污染正式运行时的数据）
        if not result.dry_run:
            if result.phase == "race_lost":
                # 本轮所有候选均竞争失败（或超时未及尝试）→ 下次扫描如仍可预订则重试
                _retry_queue.setdefault(user.id, set()).update(c.id for c in sorted_cands)
                _retry_queue_dirty = True
                logger.info(
                    "[%s] 📝 %d 套候选加入重试队列（下次扫描仍可预订时将重试）",
                    user.name, len(sorted_cands),
                )
            else:
                # 成功 / 非竞争性失败（账号冲突、未知错误等）→ 清除这批候选的重试标记
                # 不再重试：成功无需再试；其他错误换一套房也无法解决根本原因
                if user.id in _retry_queue:
                    for c in sorted_cands:
                        _retry_queue[user.id].discard(c.id)
                    _retry_queue_dirty = True

        if result.dry_run:
            logger.info("[%s] [DRY RUN] 自动预订跳过: %s", user.name, booked_listing.name)
        elif result.success:
            sent = await notifier.send_booking_success(
                booked_listing, result.message, result.pay_url, result.contract_start_date
            )
            if web_notifier:
                await web_notifier.send_booking_success(
                    booked_listing, result.message, result.pay_url, result.contract_start_date
                )
            if not sent:
                # 通知发送失败（渠道关闭/配置错误/网络问题），付款链接必须保留在日志中
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
                await web_notifier.send_booking_failed(booked_listing, result.message)

    # ── 清理预登录 Session ─────────────────────────────────────────── #
    for uid, ps in prewarmed_sessions.items():
        try:
            ps.session.close()
            logger.debug("%s 预登录 session 已关闭", uid)
        except Exception:
            pass

    # ── 持久化重试队列（仅在变更时写入）─────────────────────────── #
    if _retry_queue_dirty:
        storage.save_retry_queue(_retry_queue)
        _retry_queue_dirty = False

    logger.info(
        "本轮结束: %d 新房源（已通知 %d），%d 状态变更，数据库共 %d 条",
        len(new_listings), total_notified, len(status_changes), storage.count_all(),
    )


def _build_user_notifiers(users: list[UserConfig]) -> UserNotifiers:
    """
    为所有 enabled=True 的用户创建对应的 MultiNotifier。

    Returns
    -------
    UserNotifiers = list[(UserConfig, BaseNotifier)]
    """
    return [(u, create_user_notifier(u)) for u in users if u.enabled]


async def main_loop(
    cfg,
    storage: Storage,
    user_notifiers: UserNotifiers,
    web_notifier: WebNotifier | None = None,
) -> None:
    """
    持续运行的主循环（`python monitor.py` 默认入口）。

    循环结构
    --------
    while True:
        1. run_once()           执行一轮抓取+通知
        2. 每 HEARTBEAT_EVERY 轮发一次心跳
        3. asyncio.wait_for(_reload_event, timeout=actual_interval)
           - 超时：正常进入下一轮
           - 事件触发（SIGHUP）：热重载 cfg + users，重建 user_notifiers
        4. 未预期异常：记录并 sleep 10s，不退出进程

    热重载
    ------
    SIGHUP 信号处理器通过 loop.call_soon_threadsafe 设置 _reload_event，
    使 wait_for 提前返回。热重载完成后清除事件，继续下一轮。
    """
    global _reload_event
    _reload_event = asyncio.Event()

    round_count = 0

    # 自适应高峰间隔：从 peak_interval 出发，成功则缩短，限流则翻倍退避。
    # 非高峰时重置，确保下次高峰期从 peak_interval 重新开始探测。
    adaptive_peak: float = float(cfg.peak_interval)

    logger.info(
        "监控启动，常规间隔 %d 秒，高峰期自适应 %d–%d 秒（%s–%s 荷兰时间），城市: %s，用户: %d 个",
        cfg.check_interval, cfg.min_interval, cfg.peak_interval,
        cfg.peak_start, cfg.peak_end,
        [c.name for c in cfg.cities], len(user_notifiers),
    )
    # 启动时打印每个用户的自动预订状态，并检查通知渠道是否可用
    for user, notifier in user_notifiers:
        ab = user.auto_book
        if ab.enabled:
            mode = "⚠️  试运行（dry_run）" if ab.dry_run else "🚀 真实预订"
            logger.info(
                "自动预订 [%s]: %s  账号: %s",
                user.name, mode, ab.email or "(未设置)",
            )
            # 自动预订开启时，通知渠道必须可用，否则付款链接无法送达
            if not user.notifications_enabled:
                logger.warning(
                    "⚠️  [%s] 自动预订已开启，但该用户通知已关闭（notifications_enabled=false）！"
                    "预订成功后付款链接将无法送达，请开启通知或在日志中查找 CRITICAL 行。",
                    user.name,
                )
            elif not user.notification_channels:
                logger.warning(
                    "⚠️  [%s] 自动预订已开启，但未配置任何通知渠道！"
                    "预订成功后付款链接将无法送达，请添加 iMessage/Telegram/Email/WhatsApp 渠道。",
                    user.name,
                )

            # 检查自动预订账号密码是否填写
            if not ab.email:
                logger.warning(
                    "⚠️  [%s] 自动预订已开启，但未填写 H2S 账号邮箱！"
                    "请前往 Web 面板「用户管理」填写 AUTO_BOOK_EMAIL。",
                    user.name,
                )
            if not ab.password:
                logger.warning(
                    "⚠️  [%s] 自动预订已开启，但未填写 H2S 账号密码！"
                    "请前往 Web 面板「用户管理」填写 AUTO_BOOK_PASSWORD。",
                    user.name,
                )
        else:
            logger.info("自动预订 [%s]: 已关闭", user.name)

    while True:
        round_count += 1
        try:
            base_interval, is_peak = _get_interval(cfg)

            if is_peak:
                # 高峰期：使用自适应间隔，在 [min_interval, peak_interval] 范围内浮动
                effective_interval = max(cfg.min_interval, int(adaptive_peak))
                peak_tag = f"【高峰期 {effective_interval}s】"
            else:
                # 非高峰期：使用常规间隔，同时重置自适应（为下次高峰期做准备）
                effective_interval = base_interval
                adaptive_peak = float(cfg.peak_interval)
                peak_tag = ""

            logger.info("===== 第 %d 轮 %s=====", round_count, peak_tag)

            # booking_deadline：在此时刻后不再尝试备选房源，让下一轮扫描优先进行
            booking_deadline = time.monotonic() + effective_interval
            await run_once(cfg, storage, user_notifiers, web_notifier=web_notifier,
                           booking_deadline=booking_deadline)

            # 成功：高峰期将自适应间隔缩短 5%（逐步逼近 min_interval）
            if is_peak:
                prev = adaptive_peak
                adaptive_peak = max(float(cfg.min_interval), adaptive_peak * _ADAPTIVE_DECREASE)
                if int(prev) != int(adaptive_peak):
                    logger.info(
                        "🔽 自适应间隔: %d → %d 秒（下限 %d 秒）",
                        int(prev), int(adaptive_peak), cfg.min_interval,
                    )

            if round_count % HEARTBEAT_EVERY == 0:
                total = storage.count_all()
                for _, notifier in user_notifiers:
                    await notifier.send_heartbeat(total_in_db=total, round_count=round_count)
                if web_notifier:
                    await web_notifier.send_heartbeat(total_in_db=total, round_count=round_count)
                # 清理旧通知，防止 web_notifications 表无限增长
                pruned = storage.prune_notifications(keep=500)
                if pruned:
                    logger.debug("已清理 %d 条旧通知", pruned)

            actual = _apply_jitter(effective_interval, cfg.jitter_ratio)
            logger.info(
                "等待 %d 秒（基准 %d s，±%.0f%% 抖动）%s",
                actual, effective_interval, cfg.jitter_ratio * 100,
                "（高峰期自适应）" if is_peak else "",
            )

            # 等待下一轮：超时正常继续；SIGHUP 或 reload 文件触发则热重载。
            # Windows 不支持可靠的 SIGHUP，因此每秒轮询一次 reload 请求文件。
            reload_triggered = False
            loop = asyncio.get_running_loop()
            deadline = loop.time() + float(actual)

            while True:
                if _consume_reload_request_file():
                    logger.info("检测到文件触发的热重载请求")
                    reload_triggered = True
                    break

                remaining = deadline - loop.time()
                if remaining <= 0:
                    break

                try:
                    await asyncio.wait_for(_reload_event.wait(), timeout=min(1.0, remaining))
                    reload_triggered = True
                    break
                except (asyncio.TimeoutError, TimeoutError):
                    pass

            if reload_triggered:
                _reload_event.clear()
                logger.info("热重载中...")
                load_dotenv(dotenv_path=ENV_PATH, override=True)
                try:
                    cfg = load_config()
                    users = load_users()
                    for _, n in user_notifiers:
                        await n.close()
                    user_notifiers = _build_user_notifiers(users)
                    # 热重载后重置自适应间隔（用户可能改了 peak_interval / min_interval）
                    adaptive_peak = float(cfg.peak_interval)
                    logger.info(
                        "配置已热重载：城市=%s  用户=%d  间隔=%ds  高峰自适应=%d–%ds(%s–%s)",
                        [c.name for c in cfg.cities], len(user_notifiers),
                        cfg.check_interval, cfg.min_interval, cfg.peak_interval,
                        cfg.peak_start, cfg.peak_end,
                    )
                except Exception as e:
                    logger.error("热重载失败，继续使用旧配置: %s", e)

        except asyncio.CancelledError:
            raise  # 允许正常关闭（KeyboardInterrupt 等）
        except RateLimitError:
            # 被限流：自适应间隔翻倍退避，然后冷却 5 分钟
            prev = adaptive_peak
            adaptive_peak = min(float(cfg.check_interval), adaptive_peak * _ADAPTIVE_INCREASE)
            cooldown = _apply_jitter(300, cfg.jitter_ratio)
            logger.warning(
                "⚠️  触发限流，自适应间隔 %d → %d 秒，冷却 %d 秒后继续",
                int(prev), int(adaptive_peak), cooldown,
            )
            await asyncio.sleep(cooldown)
        except Exception as e:
            # 任何未预期异常：记录并等待 10 秒后继续，而不是静默退出
            logger.exception("主循环出现异常，10 秒后继续: %s", e)
            await asyncio.sleep(10)


# ------------------------------------------------------------------ #
# 入口
# ------------------------------------------------------------------ #

async def _async_main() -> None:
    parser = argparse.ArgumentParser(description="Holland2Stay 房源监控")
    parser.add_argument("--once", action="store_true", help="只运行一次后退出")
    parser.add_argument("--test", action="store_true", help="抓取并打印，不写库不发通知")
    parser.add_argument("--reset-db", action="store_true", help="启动前清空数据库（非交互式）")
    args = parser.parse_args()

    cfg = load_config()
    _setup_logging(cfg.log_level)

    if args.test:
        logger.info("TEST 模式：只抓取，不发通知")
        city_tasks, availability_ids = cfg.scrape_tasks()
        fresh = await asyncio.get_running_loop().run_in_executor(
            None, lambda: scrape_all(city_tasks, availability_ids)
        )
        print(json.dumps([l.to_dict() for l in fresh], ensure_ascii=False, indent=2))
        return

    # ── 数据库重置 ────────────────────────────────────────────────── #
    if args.reset_db:
        db = Storage(cfg.db_path, timezone_str=cfg.timezone)
        db.reset_all()
        db.close()
        logger.warning("数据库已清空，所有历史记录已删除")

    storage = Storage(cfg.db_path, timezone_str=cfg.timezone)

    # 恢复持久化的竞败重试队列（进程重启后不丢失）
    global _retry_queue
    _retry_queue = storage.load_retry_queue()
    if _retry_queue:
        total = sum(len(v) for v in _retry_queue.values())
        logger.info("已恢复重试队列: %d 个用户, %d 套候选", len(_retry_queue), total)

    # 加载用户配置；文件损坏时硬停止，避免迁移逻辑覆盖现有数据
    try:
        users = load_users()
    except RuntimeError as e:
        logger.critical("❌ 无法加载用户配置，进程终止以防数据丢失:\n  %s", e)
        sys.exit(1)

    # 仅在文件完全不存在时（真正的首次运行）才执行 .env 迁移。
    # users 为空列表但文件已存在，说明是有意清空，不触发迁移。
    if not USERS_FILE.exists():
        migrated = migrate_from_env()
        if migrated:
            save_users([migrated])
            users = [migrated]
            logger.info("✅ 已从 .env 迁移旧配置，创建默认用户「%s」", migrated.name)
        else:
            logger.warning(
                "⚠️  users.json 不存在且 .env 无通知配置。"
                "请在 Web 面板（python web.py）的「用户」页面添加用户。"
            )
    elif not users:
        logger.warning(
            "⚠️  users.json 为空列表，通知和自动预订不可用。"
            "请在 Web 面板添加用户。"
        )

    user_notifiers = _build_user_notifiers(users)
    if not user_notifiers:
        logger.warning("没有启用的用户，通知功能不可用（监控仍会写库）")

    # Web 面板通知：与平台无关，始终创建
    web_notifier = WebNotifier(storage)
    logger.info("Web 面板通知已启用（所有事件将写入 web_notifications 表）")

    _write_pid()
    _setup_signals(asyncio.get_running_loop())

    try:
        if args.once:
            await run_once(cfg, storage, user_notifiers, web_notifier=web_notifier)
        else:
            await main_loop(cfg, storage, user_notifiers, web_notifier=web_notifier)
    finally:
        storage.close()
        for _, n in user_notifiers:
            await n.close()
        _remove_pid()


def main() -> None:
    # Windows 默认 ProactorEventLoop 与 asyncio.wait_for + Event.wait() 有兼容问题，
    # 切换为 SelectorEventLoop 可避免超时时意外抛出 CancelledError。
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    try:
        asyncio.run(_async_main())
    except KeyboardInterrupt:
        logger.info("用户中断，退出")
        _remove_pid()
        sys.exit(0)


if __name__ == "__main__":
    main()
