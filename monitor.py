"""
Holland2Stay 房源监控主程序
============================
运行方式：
    python monitor.py           # 持续监控
    python monitor.py --once    # 只跑一次（适合 cron）
    python monitor.py --test    # 抓取并打印，不发送通知，不写数据库
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
from config import load_config
from notifier import BaseNotifier, create_user_notifier
from scraper import scrape_all
from storage import Storage
from users import UserConfig, load_users, migrate_from_env, save_users


def _setup_logging(level: str) -> None:
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(level=getattr(logging, level, "INFO"), format=fmt)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


logger = logging.getLogger("monitor")

HEARTBEAT_EVERY = 12   # 默认 5min * 12 = 1h

_PID_FILE = Path("data/monitor.pid")
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
    根据荷兰本地时间判断当前是否处于高峰期。
    返回 (interval_seconds, is_peak)。
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


# 抖动比例：实际等待时间在基准值的 ±JITTER_RATIO 范围内随机浮动
# 例如基准 300s → 实际 240~360s；基准 60s → 实际 48~72s
_JITTER_RATIO = 0.20

def _apply_jitter(seconds: int) -> int:
    """在 seconds 基础上加 ±JITTER_RATIO 的随机抖动，最小保留 5 秒。"""
    delta = seconds * _JITTER_RATIO
    return max(5, int(seconds + random.uniform(-delta, delta)))


# ------------------------------------------------------------------ #
# 核心逻辑
# ------------------------------------------------------------------ #

def _area_key(listing) -> float:
    """从 feature_map 里提取面积数值，用于自动预订候选排序。"""
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
    """执行一次完整的抓取→对比→按用户分发通知/预订流程。"""
    city_tasks, availability_ids = cfg.scrape_tasks()
    logger.info("开始抓取，城市数: %d，活跃用户数: %d", len(city_tasks), len(user_notifiers))

    try:
        fresh = await asyncio.get_event_loop().run_in_executor(
            None, lambda: scrape_all(city_tasks, availability_ids)
        )
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

    # 每个用户的自动预订候选（房源列表）
    ab_candidates: dict[str, list] = {u.id: [] for u, _ in user_notifiers}

    # ── 新房源通知 ──────────────────────────────────────────────── #
    total_notified = 0
    for listing in new_listings:
        notified_this = False
        for user, notifier in user_notifiers:
            # 收集自动预订候选
            if (
                user.auto_book.enabled
                and listing.status.lower() == "available to book"
                and (user.auto_book.listing_filter.is_empty()
                     or user.auto_book.listing_filter.passes(listing))
            ):
                ab_candidates[user.id].append(listing)

            # 通知过滤
            if not user.listing_filter.is_empty() and not user.listing_filter.passes(listing):
                logger.debug("[%s] 跳过通知（过滤条件不符）: %s", user.name, listing.name)
                continue

            logger.info("[%s] 新房源: %s (%s)", user.name, listing.name, listing.status)
            ok = await notifier.send_new_listing(listing)
            if ok:
                notified_this = True
                total_notified += 1

        if notified_this:
            storage.mark_notified(listing.id)

    # ── 状态变更通知 ──────────────────────────────────────────────── #
    for listing, old_status, new_status in status_changes:
        notified_this = False
        for user, notifier in user_notifiers:
            if (
                user.auto_book.enabled
                and new_status.lower() == "available to book"
                and (user.auto_book.listing_filter.is_empty()
                     or user.auto_book.listing_filter.passes(listing))
            ):
                ab_candidates[user.id].append(listing)

            if not user.listing_filter.is_empty() and not user.listing_filter.passes(listing):
                logger.debug("[%s] 状态变更跳过通知: %s", user.name, listing.name)
                continue

            logger.info("[%s] 状态变更: %s  %s → %s", user.name, listing.name, old_status, new_status)
            ok = await notifier.send_status_change(listing, old_status, new_status)
            if ok:
                notified_this = True

        if notified_this:
            storage.mark_status_change_notified(listing.id)

    # ── 自动预订（每个用户独立判断）─────────────────────────────── #
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
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: try_book(target, user.auto_book.email, user.auto_book.password,
                             dry_run=user.auto_book.dry_run),
        )
        if result.dry_run:
            logger.info("[%s] [DRY RUN] 自动预订跳过: %s", user.name, target.name)
        elif result.success:
            await notifier.send_booking_success(target, result.message)
        else:
            await notifier.send_booking_failed(target, result.message)

    logger.info(
        "本轮结束: %d 新房源（已通知 %d），%d 状态变更，数据库共 %d 条",
        len(new_listings), total_notified, len(status_changes), storage.count_all(),
    )


def _build_user_notifiers(users: list[UserConfig]) -> UserNotifiers:
    """为所有启用的用户创建通知器。"""
    return [(u, create_user_notifier(u)) for u in users if u.enabled]


async def main_loop(cfg, storage: Storage, user_notifiers: UserNotifiers) -> None:
    """持续运行的主循环，支持 SIGHUP 热重载配置 + 智能轮询。"""
    global _reload_event
    _reload_event = asyncio.Event()

    round_count = 0
    logger.info(
        "监控启动，常规间隔 %d 秒，高峰期间隔 %d 秒（%s–%s 荷兰时间），城市: %s，用户: %d 个",
        cfg.check_interval, cfg.peak_interval, cfg.peak_start, cfg.peak_end,
        [c.name for c in cfg.cities], len(user_notifiers),
    )
    # 启动时打印每个用户的自动预订状态，避免误以为已开启/关闭
    for user, _ in user_notifiers:
        ab = user.auto_book
        if ab.enabled:
            mode = "⚠️  试运行（dry_run）" if ab.dry_run else "🚀 真实预订"
            logger.info(
                "自动预订 [%s]: %s  账号: %s",
                user.name, mode, ab.email or "(未设置)",
            )
        else:
            logger.info("自动预订 [%s]: 已关闭", user.name)

    while True:
        round_count += 1
        try:
            interval, is_peak = _get_interval(cfg)
            peak_tag = "【高峰期】" if is_peak else ""
            logger.info("===== 第 %d 轮 %s=====", round_count, peak_tag)

            await run_once(cfg, storage, user_notifiers)

            if round_count % HEARTBEAT_EVERY == 0:
                total = storage.count_all()
                for _, notifier in user_notifiers:
                    await notifier.send_heartbeat(total_in_db=total, fresh_count=round_count)

            actual = _apply_jitter(interval)
            logger.info(
                "等待 %d 秒（基准 %d s，±%.0f%% 抖动）%s",
                actual, interval, _JITTER_RATIO * 100,
                "（高峰期加速）" if is_peak else "",
            )

            # 等待下一轮：超时正常继续；SIGHUP/事件触发则热重载
            # 同时 catch TimeoutError（builtin）以兼容 Windows SelectorEventLoop
            reload_triggered = False
            try:
                await asyncio.wait_for(_reload_event.wait(), timeout=float(actual))
                reload_triggered = True
            except (asyncio.TimeoutError, TimeoutError):
                pass

            if reload_triggered:
                _reload_event.clear()
                logger.info("热重载中...")
                load_dotenv(override=True)
                try:
                    cfg = load_config()
                    users = load_users()
                    for _, n in user_notifiers:
                        await n.close()
                    user_notifiers = _build_user_notifiers(users)
                    logger.info(
                        "配置已热重载：城市=%s  用户=%d  间隔=%ds  高峰=%ds(%s–%s)",
                        [c.name for c in cfg.cities], len(user_notifiers),
                        cfg.check_interval, cfg.peak_interval, cfg.peak_start, cfg.peak_end,
                    )
                except Exception as e:
                    logger.error("热重载失败，继续使用旧配置: %s", e)

        except asyncio.CancelledError:
            raise  # 允许正常关闭（KeyboardInterrupt 等）
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
        fresh = await asyncio.get_event_loop().run_in_executor(
            None, lambda: scrape_all(city_tasks, availability_ids)
        )
        print(json.dumps([l.to_dict() for l in fresh], ensure_ascii=False, indent=2))
        return

    storage = Storage(cfg.db_path)

    # 加载用户；若第一次运行且 .env 有旧配置，自动迁移
    users = load_users()
    if not users:
        migrated = migrate_from_env()
        if migrated:
            save_users([migrated])
            users = [migrated]
            logger.info("✅ 已从 .env 迁移旧配置，创建默认用户「%s」", migrated.name)
        else:
            logger.warning(
                "⚠️  users.json 为空且 .env 无通知配置。"
                "请在 Web 面板（python web.py）的「用户」页面添加用户。"
            )

    user_notifiers = _build_user_notifiers(users)
    if not user_notifiers:
        logger.warning("没有启用的用户，通知功能不可用（监控仍会写库）")

    _write_pid()
    _setup_signals(asyncio.get_event_loop())

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
