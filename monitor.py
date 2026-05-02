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
3. 遍历启用的用户：
   a. 按 listing_filter 决定是否发送通知
   b. 若 auto_book 启用且房源符合条件，调用 `try_book()` 完成自动预订
4. 写 meta（last_scrape_at）；每 HEARTBEAT_EVERY 轮发心跳

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
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from booker import try_book
from config import DATA_DIR, ENV_PATH, load_config
from notifier import BaseNotifier, create_user_notifier
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
    import re
    area_str = listing.feature_map().get("area", "")
    m = re.search(r"[\d]+\.?\d*", area_str)
    return float(m.group()) if m else 0.0


async def run_once(
    cfg,
    storage: Storage,
    user_notifiers: UserNotifiers,
    *,
    dry_run: bool = False,
) -> None:
    """
    执行一次完整的「抓取 → 对比 → 通知 → 自动预订」流程。

    Parameters
    ----------
    cfg            : 当前全局配置（Config 实例）
    storage        : SQLite 持久化层
    user_notifiers : [(UserConfig, BaseNotifier), ...]，启用的用户列表
    dry_run        : True 时（--test 模式）只打印结果，不写库不发通知

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
    city_tasks, availability_ids = cfg.scrape_tasks()
    logger.info("开始抓取，城市数: %d，活跃用户数: %d", len(city_tasks), len(user_notifiers))

    try:
        fresh = await asyncio.get_running_loop().run_in_executor(
            None, lambda: scrape_all(city_tasks, availability_ids)
        )
    except RateLimitError as e:
        # 429 需要更长冷却，上传给 main_loop 单独处理（不走普通 10s 恢复路径）
        logger.warning("⚠️  抓取被限流: %s", e)
        if not dry_run:
            for _, notifier in user_notifiers:
                await notifier.send_error(
                    f"⚠️ 抓取被限流（429）\n{e}\n监控将暂停 5 分钟后继续。"
                )
        raise
    except Exception as e:
        logger.error("抓取全部失败: %s", e, exc_info=True)
        if not dry_run:
            for _, notifier in user_notifiers:
                await notifier.send_error(f"抓取失败: {e}")
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

    storage.set_meta("last_scrape_at", datetime.now(timezone.utc).isoformat())
    storage.set_meta("last_scrape_count", str(len(fresh)))

    new_listings, status_changes = storage.diff(fresh)

    # ── 快速候选预扫描：立即收集候选，抢在发通知之前提交预订 ──────── #
    # 此处只做过滤判断（纯内存），不发任何通知
    ab_candidates: dict[str, list] = {u.id: [] for u, _ in user_notifiers}

    # listing.id → (old_status, new_status)：记录状态变更触发的候选，用于日志区分
    status_transition: dict[str, tuple[str, str]] = {}

    for listing in new_listings:
        for user, _ in user_notifiers:
            if (
                user.auto_book.enabled
                and listing.status.lower() == "available to book"
                and (user.auto_book.listing_filter.is_empty()
                     or user.auto_book.listing_filter.passes(listing))
            ):
                ab_candidates[user.id].append(listing)

    for listing, old_status, new_status in status_changes:
        if new_status.lower() == "available to book":
            status_transition[listing.id] = (old_status, new_status)
        for user, _ in user_notifiers:
            if (
                user.auto_book.enabled
                and new_status.lower() == "available to book"
                and (user.auto_book.listing_filter.is_empty()
                     or user.auto_book.listing_filter.passes(listing))
            ):
                ab_candidates[user.id].append(listing)

    # 立即将 try_book() 提交到线程池，返回 Future（非阻塞）
    # ab_futures: list of (user, notifier, target_listing, Future[BookingResult])
    ab_futures: list[tuple] = []
    loop = asyncio.get_running_loop()

    for user, notifier in user_notifiers:
        candidates = ab_candidates.get(user.id, [])
        if not (user.auto_book.enabled and candidates):
            continue
        target = max(candidates, key=_area_key)
        if len(candidates) > 1:
            logger.info(
                "[%s] 自动预订候选 %d 套，选面积最大: %s (%.1f m²)",
                user.name, len(candidates), target.name, _area_key(target),
            )
        # 区分：状态变更触发（快速通道）vs 新上线房源
        if target.id in status_transition:
            old_s, new_s = status_transition[target.id]
            logger.info(
                "[%s] 🚀 快速预订通道 (%s → %s)，立即提交: %s",
                user.name, old_s, new_s, target.name,
            )
        else:
            logger.info("[%s] 🔖 自动预订（新上线可预订），立即提交: %s", user.name, target.name)

        future = loop.run_in_executor(
            None,
            lambda t=target, u=user: try_book(
                t, u.auto_book.email, u.auto_book.password,
                dry_run=u.auto_book.dry_run,
            ),
        )
        ab_futures.append((user, notifier, target, future))

    # ── 新房源通知（预订任务已在后台并行运行）────────────────────── #
    total_notified = 0
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

        if notified_this:
            storage.mark_notified(listing.id)

    # ── 状态变更通知（预订任务已在后台并行运行）──────────────────── #
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

        if notified_this:
            storage.mark_status_change_notified(listing.id)

    # ── 等待预订结果，发送成功/失败通知 ──────────────────────────── #
    for user, notifier, target, future in ab_futures:
        result = await future
        if result.dry_run:
            logger.info("[%s] [DRY RUN] 自动预订跳过: %s", user.name, target.name)
        elif result.success:
            sent = await notifier.send_booking_success(
                target, result.message, result.pay_url, result.contract_start_date
            )
            if not sent:
                # 通知发送失败（渠道关闭/配置错误/网络问题），付款链接必须保留在日志中
                # 使用 CRITICAL 级别确保即使 LOG_LEVEL=WARNING 也能被看到
                logger.critical(
                    "❌ [%s] 自动预订成功但通知发送失败，付款链接已记录于此，请立即操作：\n"
                    "  房源：%s\n"
                    "  付款：%s",
                    user.name, target.name, result.pay_url,
                )
        else:
            await notifier.send_booking_failed(target, result.message)

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


async def main_loop(cfg, storage: Storage, user_notifiers: UserNotifiers) -> None:
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

            await run_once(cfg, storage, user_notifiers)

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

    storage = Storage(cfg.db_path)

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

    _write_pid()
    _setup_signals(asyncio.get_running_loop())

    try:
        if args.once:
            await run_once(cfg, storage, user_notifiers)
        else:
            await main_loop(cfg, storage, user_notifiers)
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
