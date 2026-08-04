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
1. 写心跳 meta（抓取**之前**，与成败无关——它回答的是「循环还活着吗」）
2. 逐 source 调 `dispatch_scrape_tasks()`（sync，在 executor 线程中运行），
   拿到房源 + 每个城市的完整扫描信号。一个 source 失败只隔离它自己
3. `storage.diff()` 对比库中快照，产出 new_listings / status_changes
4. 收集自动预订候选（纯内存）；**只对本轮真有候选的用户**做 H2S 预登录，
   然后立即把 try_book() 提交到线程池
5. 发送新房源/状态变更通知（与步骤 4 的预订并发进行）
6. 等待预订完成，推送预订结果通知
7. 写 meta（last_scrape_at）；按完整扫描信号决定是否做 stale 收敛；
   按时间间隔发心跳通知

完整扫描信号（completeness）不是只用来看的：只有本轮抓全了的城市才会执行
stale listing 收敛，否则「没抓到」会被误判成「已下架」。

智能轮询
--------
get_interval() 根据荷兰本地时间判断是否处于高峰期，**两个窗口**：默认工作日
8:30–10:00（PEAK_START/PEAK_END）和 13:30–15:00（PEAK_START_2/PEAK_END_2）。
高峰期使用 PEAK_INTERVAL（默认 60s）并逐轮自适应收紧，其余时间使用
CHECK_INTERVAL（默认 300s）。实际等待时间在基准值 ±JITTER_RATIO（默认 20%）
随机抖动，避免固定周期特征。

热重载
------
收到 SIGHUP 信号后，在本轮结束时重载 .env + SQLite 用户配置，无需重启进程。
Web 面板的「立即应用」按钮通过发送 SIGHUP（`kill -HUP <PID>`）触发。

依赖模块
--------
scrapers/ → storage → notifier → booker（单向，无循环）
config / users：被各模块按需 import
（顶层 `scraper.py` 只剩向后兼容的 re-export 壳，抓取实现都在 `scrapers/`）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from dotenv import load_dotenv

from booker import BookingBlockedError, PrewarmedSession
from config import DATA_DIR, ENV_PATH, get_proxy_url, is_proxy_native_fallback_active, load_config
from models import STATUS_AVAILABLE
from notifier import BaseNotifier, WebNotifier, create_user_notifier
from mcore.booking import RetryQueue, area_key, book_with_fallback
from mcore.interval import apply_jitter, get_interval
from mcore.prewarm import PrewarmCache
from scrapers import (
    BlockedError,
    ProxyError,
    RateLimitError,
    ScrapeNetworkError,
    UpstreamMaintenanceError,
    dispatch_scrape_tasks,
    is_proxy_service_error,
)
from update_checker import check_for_updates
from storage import Storage
from users import UserConfig, load_users, save_users
from models import Listing


# source -> 该 source 专属的长存单线程 executor（见 _get_browser_executor）
_browser_executors: dict[str, ThreadPoolExecutor] = {}


def _get_browser_executor(source: str) -> ThreadPoolExecutor:
    """取某个浏览器型 source 的**进程级长存**单线程 executor。

    形状由三个约束共同决定：

    1. CloakBrowser 包的是 Playwright 同步 API。在 Linux 容器里从默认
       executor 启动会继承到 asyncio 状态，被判成 "Sync API inside the
       asyncio loop" 而拒绝启动——所以不能用默认 executor。
    2. Playwright 的对象绑定创建它的线程，换线程即失效——所以线程必须活得
       比一轮长，否则浏览器无法跨轮存活（早先每轮新建又销毁线程，跨轮复用
       逻辑因此永远命不中，退化成每轮重建浏览器 + 重过一次 CF 挑战）。
    3. **两个独立的 Playwright sync 实例不能共存于同一线程。** 第一个实例会
       在该线程装上 event loop，第二个 launch() 随即撞上约束 1 的检查。
       所以不能让 H2S 和 Xior 共用一个线程——每个 source 一条。

    非浏览器 source（OurDomain）仍走默认 executor：它没有 Playwright 对象，
    挤进来只会和浏览器抢这唯一的线程。
    """
    ex = _browser_executors.get(source)
    if ex is None:
        ex = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=f"{source}-scrape"
        )
        _browser_executors[source] = ex
    return ex


async def _dispatch_scrape_tasks_async(
    loop: asyncio.AbstractEventLoop,
    selected: list,
    *,
    isolated: bool = False,
    browser_source: str = "",
    multi_source: bool = False,
):
    """
    Run the synchronous scraper dispatcher from async monitor code.

    浏览器型 source 各自走专属的长存线程（见 ``_get_browser_executor``）；
    非浏览器 source 继续用默认 executor。

    ``multi_source`` 要透传给 dispatcher：本函数每次只喂一个 source 的任务，
    dispatcher 自己看不出整轮是不是多源（见 ``_completeness_key``）。
    """
    executor = (
        _get_browser_executor(browser_source or _H2S_SOURCE) if isolated else None
    )
    return await loop.run_in_executor(
        executor,
        lambda: dispatch_scrape_tasks(selected, multi_source=multi_source),
    )


def _setup_logging(level: str) -> None:
    """
    配置主日志（monitor.log，全量 INFO+）+ 错误日志（errors.log，仅 WARNING+）。

    错误日志的存在意义
    ------------------
    monitor.log 长跑下 INFO 噪音淹没真正的告警；errors.log 单独保留
    WARNING/ERROR/CRITICAL，便于事后排查抓取失败、下单异常、限流等问题。
    - 更大的 backupCount：错误稀疏，保留更长时间窗口（10MB 历史）
    - 更详细的 formatter：含 funcName:lineno，一眼定位问题源
    - 全局 root logger 接管：所有模块的 logger.warning/error 自动入此文件
    """
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(level=getattr(logging, level, "INFO"), format=fmt)
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    from logging.handlers import RotatingFileHandler
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # 主日志（INFO+）：与之前一致，Web 面板默认查看
    main_fh = RotatingFileHandler(
        str(DATA_DIR / "monitor.log"),
        maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8",
    )
    main_fh.setFormatter(logging.Formatter(fmt))
    main_fh.setLevel(getattr(logging, level, "INFO"))
    logging.getLogger().addHandler(main_fh)

    # 错误日志（WARNING+）：抓取/下单异常的专用归档
    error_fh = RotatingFileHandler(
        str(DATA_DIR / "errors.log"),
        maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8",
    )
    error_fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s.%(funcName)s:%(lineno)d %(message)s"
    ))
    error_fh.setLevel(logging.WARNING)
    logging.getLogger().addHandler(error_fh)


logger = logging.getLogger("monitor")


def _unpack_scrape_result(result):
    """兼容测试/旧 monkeypatch 返回 list；真实 scraper 返回 (listings, completeness)。"""
    if (
        isinstance(result, tuple)
        and len(result) == 2
        and isinstance(result[1], dict)
    ):
        return result
    return result, {}


def _log_scrape_completeness(completeness: dict[str, bool]) -> None:
    """打一行本轮完整扫描概览。

    只负责日志；真正消费 completeness 的是
    ``_mark_stale_listings_for_complete_cities``（见 main_loop 的收敛分支）。
    """
    if not completeness:
        return
    complete_n = sum(1 for ok in completeness.values() if ok)
    logger.info(
        "本轮完整扫描: %d/%d 城市 (%s)",
        complete_n,
        len(completeness),
        ", ".join(f"{city}={'✓' if ok else '✗'}" for city, ok in completeness.items()),
    )


_SHARD_CURSOR_PREFIX = "shard_cursor:"


def _shard_source_tasks(
        tasks: list,
        source: str,
        size: int,
        cursor: int,
) -> tuple[list, int]:
    """取某个 source 本轮该抓的切片，返回 ``(切片, 下一轮游标)``。

    纯函数，游标读写留给调用方——这样分片规则本身能脱离 DB 测试。

    ``size <= 0`` 或 ``size >= len(tasks)`` 时不分片（原样返回）：配置成
    「每轮 5 个」而实际只有 4 个 target 时，行为必须与没配过一样。

    切片会绕回开头，所以 target 数不是 size 整数倍时也能均匀覆盖，不会让
    末尾那几个永远排在同一轮。
    """
    n = len(tasks)
    if size <= 0 or n == 0 or size >= n:
        return tasks, 0
    start = cursor % n
    idx = [(start + i) % n for i in range(size)]
    return [tasks[i] for i in idx], (start + size) % n


_SOURCE_LAST_SCRAPE_PREFIX = "source_last_scrape:"


def _apply_source_intervals(
        tasks: list,
        cfg,
        storage: Storage,
        *,
        now: float | None = None,
        dry_run: bool = False,
) -> list:
    """按 ``cfg.source_min_intervals`` 跳过「刚抓过」的 source。

    和分片解决的**不是同一个问题**：分片管「每轮抓几个 target」，这个管
    「同一个 target 多久被打一次」。

    2026-08-04 生产实测的教训：Xior 从 30 栋缩到 4 栋后，分片 3/轮 等于每栋楼
    几乎每轮都被抓，而高峰时段轮次间隔只有 60–90 秒——单栋楼的请求频率涨了约
    10 倍，直接撞进限流（持续 429，单轮从 40 秒拖到 270 秒）。**楼栋数变少反而
    更容易被限流**，因为限流按单个 target 被打的频率算，30 栋轮着抓时每栋自然
    稀疏，4 栋轮着抓就全挤在一起了。

    时间戳存 meta，重启后仍然生效——否则频繁重启会绕过节流，正是限流最狠的
    时候（重启往往就是因为出问题了）。

    任何异常都回退成「不跳过」：宁可多抓一轮，也不能因为读写 meta 失败就把
    整个 source 静默停掉。
    """
    intervals = getattr(cfg, "source_min_intervals", None) or {}
    if not intervals:
        return tasks

    now = time.time() if now is None else now
    out: list = []
    by_source: dict[str, list] = {}
    for t in tasks:
        by_source.setdefault(t.source, []).append(t)

    for src, group in by_source.items():
        gap = int(intervals.get(src, 0) or 0)
        if gap <= 0:
            out.extend(group)
            continue
        key = _SOURCE_LAST_SCRAPE_PREFIX + src
        try:
            last = float(storage.get_meta(key, default="") or 0)
        except (TypeError, ValueError):
            last = 0.0
        except Exception:
            logger.debug("读取 %s 上次抓取时刻失败，本轮照常抓", src, exc_info=True)
            out.extend(group)
            continue

        waited = now - last
        # last <= 0 = 从没抓过（或时间戳读坏了），必须放行——否则首轮就被跳过。
        # waited < 0 = 时间戳在未来（改过系统时间/时钟回拨），也放行，
        # 不能让 source 卡到时间追上为止。
        if last > 0 and 0 <= waited < gap:
            logger.info(
                "source %s 距上次抓取仅 %.0f 秒（< %d 秒），本轮跳过"
                "——抓太频繁会撞限流，反而更慢",
                src, waited, gap,
            )
            continue

        if not dry_run:
            try:
                storage.set_meta(key, str(now))
            except Exception:
                # 写不进去就不节流：否则时间戳永远是旧的，每轮都照抓，
                # 至少行为退化成「和没配一样」，而不是把 source 停掉。
                logger.warning("写 %s 抓取时刻失败，本轮不节流", src, exc_info=True)
        out.extend(group)
    return out


def _apply_task_sharding(
        tasks: list,
        cfg,
        storage: Storage,
        *,
        dry_run: bool = False,
) -> list:
    """按 ``cfg.shard_sizes`` 把 target 多的 source 拆到多轮抓。

    为什么必须有这个：Xior 的请求间隔是 5s（限流按速率算，调小会直接撞回
    429），实测每栋楼 13.9 秒。官方注册表 30 栋 ≈ 417 秒/轮，而 CHECK_INTERVAL
    是 300 秒；更糟的是 H2S 排在其它 source **之后**执行，不分片等于每轮把真正
    出房源的那个 source 推迟 7 分钟。

    游标存 meta，重启后接着转——否则每次重启都从第一片开始，后面的楼栋会被
    系统性地少抓。

    任何异常都回退成「不分片」：宁可这一轮慢，也不能悄悄漏抓楼栋。
    """
    sizes = getattr(cfg, "shard_sizes", None) or {}
    if not sizes:
        return tasks

    by_source: dict[str, list] = {}
    for t in tasks:
        by_source.setdefault(t.source, []).append(t)

    out: list = []
    for src, group in by_source.items():
        size = int(sizes.get(src, 0) or 0)
        if size <= 0 or size >= len(group):
            out.extend(group)
            continue
        key = _SHARD_CURSOR_PREFIX + src
        try:
            cursor = int(storage.get_meta(key, default="") or 0)
        except (TypeError, ValueError):
            cursor = 0
        except Exception:
            logger.debug("读取 %s 分片游标失败，本轮不分片", src, exc_info=True)
            out.extend(group)
            continue

        picked, next_cursor = _shard_source_tasks(group, src, size, cursor)
        if not dry_run:
            try:
                storage.set_meta(key, str(next_cursor))
            except Exception:
                # 游标写不进去就不分片：否则每轮都从同一个位置切，后面的楼栋永远抓不到
                logger.warning("写 %s 分片游标失败，本轮改为全量抓取", src, exc_info=True)
                out.extend(group)
                continue
        logger.info(
            "source %s 分轮抓取：本轮 %d/%d 个 target（%s），下轮游标 %d",
            src, len(picked), len(group),
            ", ".join(t.city_display for t in picked), next_cursor,
        )
        out.extend(picked)
    return out


def _completeness_stats(completeness: dict[str, bool]) -> tuple[int, int]:
    """把 completeness 字典压成 (完整数, 总数)，用于轮次遥测。"""
    return sum(1 for ok in completeness.values() if ok), len(completeness)


def _record_source_round(
        storage: Storage,
        *,
        round_at: str,
        source: str,
        listings: int = 0,
        targets: int = 0,
        complete: int = 0,
        started_at: float | None = None,
        error: BaseException | None = None,
        error_type: str = "",
        error_msg: str = "",
        total_targets: int = 0,
) -> None:
    """给 ``round_stats`` 记一行。

    每个 source 跑完就立即写，不攒到整轮结束——「整轮全失败」恰恰是最该留痕的
    情形，而那条路径会直接上抛，攒着的记录就丢了。

    失败可以用 ``error=`` 传异常（类名即 error_type），也可以用
    ``error_type=`` / ``error_msg=`` 直接写——后者是给「没有异常对象、但确实
    没抓成」的情形用的，比如熔断期跳过。

    storage 写失败不上抛（``record_round_stat`` 内部已吞），这里再兜一层是防
    round_at 之类的参数构造本身出错。观测不该把被观测的东西弄崩。
    """
    try:
        duration_ms = (
            int((time.monotonic() - started_at) * 1000) if started_at is not None else 0
        )
        if error is not None:
            error_type = error_type or type(error).__name__
            error_msg = error_msg or str(error)
        storage.record_round_stat(
            round_at=round_at,
            source=source,
            listings=listings,
            targets=targets,
            complete=complete,
            duration_ms=duration_ms,
            error_type=error_type,
            error_msg=error_msg,
            total_targets=total_targets,
        )
    except Exception:
        logger.debug("轮次遥测记录失败（已忽略）", exc_info=True)


def _monitored_pairs(cfg) -> list[tuple[str, str]]:
    """当前配置里全部 (source, city) 抓取目标。

    给 ``mark_stale_listings`` 判断"哪些城市已经彻底不监控了"用。读配置失败
    时返回空列表 → 孤儿收敛整条跳过（fail-open）。绝不能因为一次配置读取
    异常就把整库判成孤儿。
    """
    try:
        return [(t.source, t.city_display) for t in cfg.scrape_tasks_v2()]
    except Exception:
        logger.debug("读取监控目标失败，跳过孤儿收敛", exc_info=True)
        return []


def _mark_stale_listings_for_complete_cities(
        storage: Storage,
        completeness: dict[str, bool],
        *,
        days: int = 7,
        lottery_days: int = 2,
        monitored_pairs: list[tuple[str, str]] | None = None,
        orphan_days: int = 30,
) -> int:
    """只对本轮完整扫描成功的城市执行 stale listing 收敛。

    key 带 ``source:`` 前缀时按 (source, city) 精确限定，避免用一个 source
    的完整性去收敛另一个 source 的同名城市。

    ``monitored_pairs`` 是**配置里**的全部目标，和上面那个"本轮扫全了的"
    不是一回事——分片和节流会让正常监控的城市这轮不出现。它只用于第二条
    路径：把已经彻底移出监控的城市里的鬼影 listing 也收敛掉。
    """
    complete_cities: list[str] = []
    complete_source_cities: list[tuple[str, str]] = []
    for key, ok in completeness.items():
        if not ok:
            continue
        if ":" in key:
            source, city = key.split(":", 1)
            complete_source_cities.append((source, city))
        else:
            complete_cities.append(key)

    if not complete_cities and not complete_source_cities:
        logger.info("跳过 stale listing 状态收敛：本轮无完整扫描城市")
        return 0

    labels = sorted(complete_cities + [f"{s}:{c}" for s, c in complete_source_cities])
    logger.info(
        "执行 stale listing 状态收敛：完整城市 %s",
        ", ".join(labels),
    )
    return storage.mark_stale_listings(
        days=days,
        lottery_days=lottery_days,
        cities=complete_cities if complete_cities else None,
        source_city_pairs=complete_source_cities if complete_source_cities else None,
        monitored_pairs=monitored_pairs,
        orphan_days=orphan_days,
    )


def _stale_sweep_decision(
    completeness: dict[str, bool],
    last_sweep_at: float,
    interval_sec: float,
    *,
    now: float,
) -> str:
    """本轮该不该跑 stale 收敛。返回 ``"run"`` / ``"wait"`` / ``"defer"``。

    ``defer`` = 到点了、但本轮一个完整扫描城市都没有。这时**不能重置计时器**：
    run_once 的兜底路径（未分类错误 / 管线错误）和 H2S 熔断期都会返回空
    completeness，24 小时那一次刚好撞上就会白白跳过，鬼影 listing 再多挂一天。
    """
    if now - last_sweep_at < interval_sec:
        return "wait"
    if not any(completeness.values()):
        return "defer"
    return "run"


def _drop_shadow_sources(
    cfg,
    new_listings: list["Listing"],
    status_changes: list[tuple["Listing", str, str]],
) -> tuple[list["Listing"], list[tuple["Listing", str, str]]]:
    """滤掉影子 source 的通知事件（房源本身已经入库，这里只拦「告诉谁」）。

    影子 source 用于新平台上线前的静默验证：照常抓取、写库、参与 stale 收敛
    和面板统计，但不发用户渠道通知、不写面板 notification feed、不推 APNs/FCM。

    ``cfg.shadow_sources`` 为空时零开销直接返回原对象。
    """
    shadow = {s.lower() for s in getattr(cfg, "shadow_sources", None) or ()}
    if not shadow:
        return new_listings, status_changes

    def _shadowed(listing) -> bool:
        return (getattr(listing, "source", "") or "").lower() in shadow

    kept_new = [l for l in new_listings if not _shadowed(l)]
    kept_sc = [t for t in status_changes if not _shadowed(t[0])]

    dropped_new = len(new_listings) - len(kept_new)
    dropped_sc = len(status_changes) - len(kept_sc)
    if dropped_new or dropped_sc:
        logger.info(
            "🔇 影子 source %s：%d 条新房源 + %d 条状态变更已入库但不通知",
            ",".join(sorted(shadow)), dropped_new, dropped_sc,
        )
    return kept_new, kept_sc


def _task_labels(tasks) -> list[str]:
    return [f"{t.source}:{t.city_display}" for t in tasks]


def _listing_booking_key(listing: Listing) -> tuple[str, str]:
    """自动预订去重键：同 source + id 的房源每轮只允许一个用户尝试。"""
    source = (getattr(listing, "source", "") or "holland2stay").strip().lower()
    return source, str(listing.id)


def _assign_auto_book_candidates(
    raw_candidates: dict[str, list[Listing]],
    user_notifiers: UserNotifiers,
) -> dict[str, list[Listing]]:
    """
    把自动预订候选从“每用户匹配”收敛成“每房源唯一归属”。

    多用户模式下，同一套房源可能同时满足多个用户的自动预订条件。直接让所有
    用户并发预订同一套房源会制造无意义的竞态，也更容易触发平台风控。这里在
    提交 executor 前做一次进程内分配：同一 listing 每轮只交给一个用户；多套
    listing 同时出现时按当前已分配数量做简单均衡，平局按用户配置顺序决定。
    """
    assigned: dict[str, list[Listing]] = {u.id: [] for u, _ in user_notifiers}
    user_order = {u.id: idx for idx, (u, _) in enumerate(user_notifiers)}
    user_names = {u.id: u.name for u, _ in user_notifiers}
    assigned_count = {u.id: 0 for u, _ in user_notifiers}

    by_listing: dict[tuple[str, str], tuple[Listing, list[str]]] = {}
    for user, _ in user_notifiers:
        seen_for_user: set[tuple[str, str]] = set()
        for listing in raw_candidates.get(user.id, []):
            key = _listing_booking_key(listing)
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

# 自适应轮询参数（固定，不需要用户配置）
# 每轮成功后将当前间隔乘以此系数（5% 缩短），缓慢逼近 min_interval
_ADAPTIVE_DECREASE = 0.95
# 遭遇 429 后将当前间隔乘以此系数（翻倍），快速退避
_ADAPTIVE_INCREASE = 2.0

# Cloudflare 403 屏蔽冷却时间（秒）。比 429 的 5 min 更长 —— 等待无法自动恢复，
# 给用户/运维时间换代理或重启进程。
_BLOCKED_COOLDOWN = 900  # 15 分钟
_BLOCKED_COOLDOWN_MAX = 7200  # 连续 Cloudflare 403 时最长冷却 2 小时

# 平台维护冷却时间（秒）。H2S 公告通常 1–2 小时窗口，15 分钟一次再探即可：
# 探到还在维护 → 继续冷却；探到恢复 → 当轮成功，正常回到 check_interval。
# 不发用户告警，不计入 network_fail_streak，安静等。
_MAINTENANCE_COOLDOWN = 900  # 15 分钟

# 连续网络失败阈值：连续 N 次全部城市第 1 页网络失败时触发冷却，
# 避免坏代理/断网时监控空转刷屏 error log
_NETWORK_FAIL_THRESHOLD = 3
_NETWORK_FAIL_COOLDOWN = 300  # 5 分钟
_PROXY_CONFIRM_RETRY_DELAY = 60  # 单次代理故障先短冷却复核，不立刻切换/直连
_NATIVE_PROXY_FALLBACK_INTERVAL = 600  # 10 分钟；代理全挂时直连原生 IP 的最高频率
_H2S_SOURCE = "holland2stay"
_PREWARM_CANDIDATE_WAIT_SEC = 2.0  # 有真实候选时最多等 2s 让预登录赶上快速通道
_H2S_LOGIN_BLOCKED_SUPPRESS_SEC = 3600  # H2S 登录/预订 403 后 1 小时内不碰登录链路
_H2S_CIRCUIT_BASE_COOLDOWN = 1800  # H2S 抓取 403 后 30 分钟后做 canary
_H2S_CIRCUIT_MAX_COOLDOWN = 21600  # H2S 抓取连续 403 时最多暂停 6 小时
_H2S_LONG_BLOCK_STREAK = 3  # 第 3 次连续 H2S 403 起视为长时间被 block
_H2S_LONG_BLOCK_NOTIFY_INTERVAL = 21600  # 长时间 block admin 告警 6 小时最多一次

# 屏蔽通知节流：避免每轮抓取都给用户推一次相同的告警。
# 状态是模块级，进程重启后清零（重启后第一轮屏蔽会再发通知，符合预期）。
_last_block_notify_at: float = 0.0
_BLOCK_NOTIFY_INTERVAL = 1800  # 30 分钟
_last_h2s_long_block_notify_at: float = 0.0

# 维护通知节流：admin 已经在 dashboard banner 上看到维护态了，再叠加 web 通知
# 主要是让 admin 在收 push（如果接了）/ 刷通知面板时也能看到一条记录。
# 间隔比屏蔽长一截——维护态用户什么都做不了，没必要 30 min 一刷。
_last_maintenance_notify_at: float = 0.0
_MAINTENANCE_NOTIFY_INTERVAL = 3600  # 1 小时


def _should_notify_block() -> bool:
    """是否该发屏蔽通知。30 分钟最多一次，避免持续屏蔽时刷屏。"""
    global _last_block_notify_at
    now = time.monotonic()
    if _last_block_notify_at <= 0 or now - _last_block_notify_at >= _BLOCK_NOTIFY_INTERVAL:
        _last_block_notify_at = now
        return True
    return False


def _should_notify_h2s_long_block() -> bool:
    """H2S 长时间 403 后是否该通知 admin。6 小时最多一次。"""
    global _last_h2s_long_block_notify_at
    now = time.monotonic()
    if (
        _last_h2s_long_block_notify_at <= 0
        or now - _last_h2s_long_block_notify_at >= _H2S_LONG_BLOCK_NOTIFY_INTERVAL
    ):
        _last_h2s_long_block_notify_at = now
        return True
    return False


def _should_notify_maintenance() -> bool:
    """是否该给 admin 发维护通知。1 小时最多一次。"""
    global _last_maintenance_notify_at
    now = time.monotonic()
    if (
        _last_maintenance_notify_at <= 0
        or now - _last_maintenance_notify_at >= _MAINTENANCE_NOTIFY_INTERVAL
    ):
        _last_maintenance_notify_at = now
        return True
    return False


# 代理失效通知节流：代理挂了 admin 也只需要知道一次，30 min 一条够。
_last_proxy_notify_at: float = 0.0
_PROXY_NOTIFY_INTERVAL = 1800  # 30 分钟


def _should_notify_proxy() -> bool:
    """是否该给 admin 发代理失效通知。30 分钟最多一次。"""
    global _last_proxy_notify_at
    now = time.monotonic()
    if _last_proxy_notify_at <= 0 or now - _last_proxy_notify_at >= _PROXY_NOTIFY_INTERVAL:
        _last_proxy_notify_at = now
        return True
    return False


# 未分类/管线错误通知节流：某个反复抛错的内部异常（如 DB 锁死、磁盘满）若每轮
# 都通知会刷屏 admin。和代理/屏蔽一样 30 min 一条。
_last_internal_notify_at: float = 0.0
_INTERNAL_NOTIFY_INTERVAL = 1800  # 30 分钟


def _should_notify_internal() -> bool:
    """是否该给 admin 发未分类/管线内部错误通知。30 分钟最多一次。"""
    global _last_internal_notify_at
    now = time.monotonic()
    if (
        _last_internal_notify_at <= 0
        or now - _last_internal_notify_at >= _INTERNAL_NOTIFY_INTERVAL
    ):
        _last_internal_notify_at = now
        return True
    return False


def _h2s_login_suppressed_remaining() -> int:
    """H2S 登录/预订链路是否因 403 被临时抑制；返回剩余秒数。"""
    return max(0, int(_h2s_login_blocked_until - time.monotonic()))


def _mark_h2s_login_blocked(reason: BaseException | str) -> None:
    """H2S 登录/预订遇到 Cloudflare 403 后，短期内停止触碰登录链路。"""
    global _h2s_login_blocked_until
    _h2s_login_blocked_until = max(
        _h2s_login_blocked_until,
        time.monotonic() + _H2S_LOGIN_BLOCKED_SUPPRESS_SEC,
    )
    prewarm_cache.clear()
    logger.warning(
        "🚫 H2S 登录/预订遇到 Cloudflare WAF，未来 %d 秒内暂停登录链路: %s",
        _H2S_LOGIN_BLOCKED_SUPPRESS_SEC,
        reason,
    )


def _h2s_circuit_remaining() -> int:
    """H2S 抓取 circuit breaker 剩余暂停秒数。"""
    return max(0, int(_h2s_circuit_open_until - time.monotonic()))


def _mark_h2s_scrape_blocked(reason: BaseException | str) -> int:
    """H2S 抓取被 Cloudflare 403 后，打开 source-level circuit breaker。"""
    global _h2s_circuit_fail_streak, _h2s_circuit_open_until, _h2s_circuit_reason
    _h2s_circuit_fail_streak += 1
    cooldown = min(
        _H2S_CIRCUIT_MAX_COOLDOWN,
        _H2S_CIRCUIT_BASE_COOLDOWN * (2 ** max(0, _h2s_circuit_fail_streak - 1)),
    )
    _h2s_circuit_open_until = time.monotonic() + cooldown
    _h2s_circuit_reason = str(reason)
    _mark_h2s_login_blocked(reason)
    logger.error(
        "🚫 H2S source 熔断：连续抓取 403=%d，暂停 H2S %d 秒；其他 source 继续运行。原因: %s",
        _h2s_circuit_fail_streak,
        cooldown,
        reason,
    )
    return cooldown


def _mark_h2s_scrape_recovered() -> None:
    """H2S canary 成功后关闭 circuit breaker，下一轮恢复完整 H2S 抓取。"""
    global _h2s_circuit_fail_streak, _h2s_circuit_open_until, _h2s_circuit_reason
    if _h2s_circuit_fail_streak or _h2s_circuit_open_until:
        logger.info(
            "✅ H2S canary 抓取成功，关闭 source 熔断（之前连续 403=%d）",
            _h2s_circuit_fail_streak,
        )
    _h2s_circuit_fail_streak = 0
    _h2s_circuit_open_until = 0.0
    _h2s_circuit_reason = ""


# 需要浏览器传输层的 source。它们的 Playwright 对象绑定创建线程，因此**必须**
# 各自跑在 ``_get_browser_executor(source)`` 的专属长存单线程上；放到默认 executor
# 里会因线程漂移抛 ``greenlet.error: Cannot switch to a different thread``。
_BROWSER_SOURCES = frozenset({"holland2stay", "xior"})


# 全部 source 都失败时，挑一个最有代表性的异常上抛。顺序 = 「哪个根因更值得
# main_loop 据以决策」：
#   ProxyError    有明确修复动作（切备用代理 / 降级直连），而且全员失败时它
#                 大概率就是其它 source 失败的共同根因 —— 排最前
#   Maintenance   平台自己会恢复，安静长冷却、不告警用户
#   Blocked       长冷却 + 指数退避
#   RateLimit     短冷却 + 自适应间隔翻倍
#   Network       连续失败计数
# ProxyError 是 ScrapeNetworkError 子类，必须排在它前面才匹配得到。
_ROUND_FAILURE_PRIORITY: tuple[type[BaseException], ...] = (
    ProxyError,
    UpstreamMaintenanceError,
    BlockedError,
    RateLimitError,
    ScrapeNetworkError,
)


def _pick_round_failure(failures: list[tuple[str, Exception]]) -> Exception:
    """从各 source 的失败里挑一个上抛给 main_loop。"""
    for cls in _ROUND_FAILURE_PRIORITY:
        for _, exc in failures:
            if isinstance(exc, cls):
                return exc
    return failures[0][1]


def _split_h2s_tasks(tasks) -> tuple[list, list]:
    """拆分 H2S 与其它 source 任务，便于只熔断 H2S。"""
    h2s_tasks = [t for t in tasks if t.source == _H2S_SOURCE]
    other_tasks = [t for t in tasks if t.source != _H2S_SOURCE]
    return other_tasks, h2s_tasks


def _select_h2s_tasks_for_circuit(h2s_tasks: list) -> tuple[list, str]:
    """
    根据 H2S circuit 状态选择本轮 H2S 任务。

    Returns
    -------
    (selected_tasks, mode)
      normal  : circuit 未打开，抓全部 H2S 城市
      open    : circuit 冷却中，不抓 H2S
      canary  : 冷却到期，只抓 1 个 H2S 城市探测恢复
    """
    if not h2s_tasks:
        return [], "none"
    remaining = _h2s_circuit_remaining()
    if remaining > 0:
        logger.warning(
            "🚫 H2S source 熔断中，跳过 %d 个 H2S 任务，%d 秒后 canary。最近原因: %s",
            len(h2s_tasks),
            remaining,
            _h2s_circuit_reason or "unknown",
        )
        return [], "open"
    if _h2s_circuit_fail_streak > 0 or _h2s_circuit_open_until > 0:
        logger.warning(
            "🚫 H2S source 熔断到期，本轮只用 1 个城市做 canary: %s",
            h2s_tasks[0].city_display,
        )
        return h2s_tasks[:1], "canary"
    return h2s_tasks, "normal"


_PID_FILE = DATA_DIR / "monitor.pid"
_RELOAD_REQUEST_FILE = DATA_DIR / "monitor.reload"

# 热重载事件（SIGHUP → 唤醒 main_loop 中的 sleep，立即重载配置）
_reload_event: asyncio.Event | None = None

# 类型别名：每个用户与其对应通知器的配对列表
UserNotifiers = list[tuple[UserConfig, BaseNotifier]]

# mcore 服务实例（进程生命周期内单例）
retry_queue = RetryQueue()
prewarm_cache = PrewarmCache()
_h2s_login_blocked_until: float = 0.0
_h2s_circuit_open_until: float = 0.0
_h2s_circuit_fail_streak: int = 0
_h2s_circuit_reason: str = ""


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

    def _handler(_signum: int, _frame: object) -> None:
        if _reload_event is not None:
            loop.call_soon_threadsafe(_reload_event.set)
            logger.info("收到 SIGHUP，将在本轮结束后热重载配置")

    try:
        signal.signal(signal.SIGHUP, _handler)
    except (OSError, AttributeError):
        logger.debug("SIGHUP 不可用（非 Unix 系统），跳过信号注册")


# ------------------------------------------------------------------ #
# 核心逻辑
# ------------------------------------------------------------------ #
#
# run_once() 是一轮「抓取 → 对比 → 通知 → 自动预订」的编排器。各阶段拆成
# 下面这组 _round_* 辅助函数，run_once 只负责串联与错误分支控制流。
# 拆分原则：纯/有界副作用的段落抽出去；raise / return 等控制流留在 run_once。


async def _broadcast_error(
    user_notifiers: "UserNotifiers",
    web_notifier: "WebNotifier | None",
    msg: str,
) -> None:
    """把一条错误文案广播给所有用户渠道 + web 面板。

    抓取异常分支里 "for notifier: send_error / if web_notifier: send_error"
    这段重复了三次，抽出来消除重复。"""
    for _, notifier in user_notifiers:
        await notifier.send_error(msg)
    if web_notifier:
        await web_notifier.send_error(msg)


async def _notify_admin_only(
    storage: "Storage",
    web_notifier: "WebNotifier | None",
    msg: str,
    *,
    kind: str,
) -> None:
    """把一条错误只发给 admin（web 面板 + admin push），不打扰普通用户。

    用于普通用户无法采取行动的内部/系统级错误（代理失效、数据库/通知管线
    故障、未分类异常）。两个渠道各自吞异常——告警通道本身不该再把 run_once
    带崩。"""
    if web_notifier:
        try:
            await web_notifier.send_error(msg)
        except Exception:
            logger.debug("admin web 告警发送失败（已忽略）", exc_info=True)
    from mcore import push as _push
    try:
        await _push.dispatch_admin(storage, msg, kind=kind)
    except Exception:
        logger.debug("admin push 告警发送失败（已忽略）", exc_info=True)


async def _dispatch_watchdog_alerts(
        storage: "Storage",
        web_notifier: "WebNotifier | None",
) -> int:
    """跑一次退化告警巡检，把该发的发给 admin。返回发出条数。

    只发 admin：完整扫描率下滑、解析器可能坏了这类事，普通用户既看不懂也做不了
    什么，发给他们只是制造焦虑。

    节流和恢复判定都在 ``watchdog.poll()`` 里做完了（状态存 meta，重启后仍生效），
    这里只负责投递。整个函数吞异常——告警通道不该把 monitor 带崩。

    调用点在 main_loop 的正常路径上，**run_once 上抛时不会执行**。这是有意的：
    run_once 只在「所有 source 都失败」时上抛，而那种情况 main_loop 自己的
    network / blocked 连续失败计数已经在告警了，这里再报一遍只是重复。遥测行
    在上抛之前就已经写好，所以恢复后的第一轮巡检仍能看到这段历史。

    也没做成 ``finally``：那样关停时（CancelledError）也会跑一次 DB 写 + 推送。
    """
    try:
        from mcore import watchdog

        alerts = watchdog.poll(storage)
        if not alerts:
            return 0
        for a in alerts:
            log = logger.info if a.level == watchdog.LEVEL_RECOVERED else logger.warning
            log("watchdog[%s] %s", a.level, a.title)
            # kind 带上 alert key：push 的 dedup 键是 (admin, kind, kind)，
            # 所有 watchdog 告警共用一个 kind 的话，同一轮里「xior 挂了」和
            # 「h2s 完整率低」会被压成一条。
            await _notify_admin_only(
                storage, web_notifier, a.message(), kind=f"watchdog:{a.key}",
            )
        return len(alerts)
    except Exception:
        logger.debug("watchdog 巡检失败（已忽略）", exc_info=True)
        return 0


def _print_dry_run(fresh: list["Listing"], user_notifiers: "UserNotifiers") -> None:
    """--test 模式打印抓取结果，不写库不发通知。

    flush=True：管道/重定向环境确保即时输出。非 ASCII 字符（房源名含荷兰语
    特殊字母）在某些终端编码下可能抛 UnicodeEncodeError，跳过不崩。"""
    def _safe_print(*args, **kw):
        try:
            print(*args, **kw)
        except UnicodeEncodeError:
            print(*(str(a).encode("ascii", "replace").decode() for a in args), **kw)

    _safe_print(f"\n{'=' * 60}", flush=True)
    _safe_print(f"[DRY RUN] 抓取结果（共 {len(fresh)} 条）", flush=True)
    for user, _ in user_notifiers:
        if not user.listing_filter.is_empty():
            matched = [l for l in fresh if user.listing_filter.passes(l)]
            _safe_print(f"  用户 [{user.name}] 过滤后符合：{len(matched)} 条", flush=True)
    _safe_print('=' * 60, flush=True)
    for l in fresh:
        _safe_print(f"  [{l.status:22s}] {l.price_display:7s} | {l.available_from or '?':12s} | {l.name}", flush=True)
    _safe_print('=' * 60, flush=True)


def _clear_maintenance_meta_if_recovered(storage: Storage) -> None:
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


# 目前只有 H2S 的预订流程真正跑通过（2026-05-22 真实下单成功，见 ARCHITECTURE §7）。
#
# **Xior 先不放进来**：bookers/rentcafe.py 里第 3 步之后的多步表单是没走过流程
# 硬猜出来的草稿，放开等于拿用户的真实账号去提交半懂不懂的表单。等流程侦察完
# 并验证过再加 "xior"。凭据判定（_can_auto_book）已经就位，到时只改这个元组。
_AUTO_BOOK_SOURCES: tuple[str, ...] = ("holland2stay",)


def _can_auto_book(user, listing) -> bool:
    """该用户能不能对这条 listing 触发自动预订。

    除了「这个 source 的预订流程实现了没有」，还要回答「用户有没有**这栋楼**的
    账号」——Xior 是一栋楼一个账号（每栋楼是独立的 RENTCafe property 门户），
    没有该楼凭据就不该产生候选。凭据本身就是开关，不需要额外的 per-building 开关。
    """
    if listing.source not in _AUTO_BOOK_SOURCES:
        return False
    if listing.source == "xior":
        from scrapers.xior import building_key_for
        key = building_key_for(listing)
        if not key:
            logger.debug("Xior listing %s 无法反查楼栋，跳过自动预订", listing.id)
            return False
        email, password = user.auto_book.xior_account_for(key)
        return bool(email and password)
    return True


def _collect_booking_candidates(
    new_listings: list["Listing"],
    status_changes: list[tuple["Listing", str, str]],
    fresh: list["Listing"],
    user_notifiers: "UserNotifiers",
) -> tuple[dict[str, list["Listing"]], dict[str, tuple[str, str]]]:
    """纯内存收集每个用户的自动预订候选（不发任何通知 / 不触网）。

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
    ab_candidates: dict[str, list["Listing"]] = {u.id: [] for u, _ in user_notifiers}
    status_transition: dict[str, tuple[str, str]] = {}

    for listing in new_listings:
        for user, notifier in user_notifiers:
            if (
                    user.auto_book.enabled
                    and user.notifications_enabled
                    and notifier.has_channels
                    and _can_auto_book(user, listing)
                    and listing.status.lower() == STATUS_AVAILABLE
                    and (user.auto_book.listing_filter.is_empty()
                         or user.auto_book.listing_filter.passes(listing))
            ):
                ab_candidates[user.id].append(listing)

    for listing, old_status, new_status in status_changes:
        if new_status.lower() == STATUS_AVAILABLE:
            status_transition[listing.id] = (old_status, new_status)
        for user, notifier in user_notifiers:
            if (
                    user.auto_book.enabled
                    and user.notifications_enabled
                    and notifier.has_channels
                    and _can_auto_book(user, listing)
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
        if l.source in _AUTO_BOOK_SOURCES and l.status.lower() == STATUS_AVAILABLE
    }
    for user, notifier in user_notifiers:
        if not user.auto_book.enabled or not user.notifications_enabled or not notifier.has_channels:
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
            if not _can_auto_book(user, listing):
                continue
            if user.auto_book.listing_filter.is_empty() or user.auto_book.listing_filter.passes(listing):
                ab_candidates[user.id].append(listing)
                logger.info(
                    "[%s] 🔁 重试 race_lost 房源（仍可预订）: %s",
                    user.name, listing.name,
                )

    ab_candidates = _assign_auto_book_candidates(ab_candidates, user_notifiers)
    return ab_candidates, status_transition


def _submit_bookings(
    loop: asyncio.AbstractEventLoop,
    ab_candidates: dict[str, list["Listing"]],
    user_notifiers: "UserNotifiers",
    prewarm_cached: dict[str, "PrewarmedSession"],
    prewarm_futures: dict[str, "asyncio.Future"],
    status_transition: dict[str, tuple[str, str]],
    booking_deadline: float,
) -> list[tuple]:
    """把每个用户的候选 book_with_fallback() 立即提交线程池（快速下单通道）。

    预订请求在发出通知之前就进入 Holland2Stay 服务器（节省 1-3 秒）。

    Returns
    -------
    ab_futures: [(user, notifier, sorted_candidates, Future, prewarmed), ...]
      sorted_candidates 按面积降序；fallback 逻辑在线程内按序尝试。
    """
    ab_futures: list[tuple] = []

    for user, notifier in user_notifiers:
        candidates = ab_candidates.get(user.id, [])
        if not (user.auto_book.enabled and candidates):
            continue
        suppressed = _h2s_login_suppressed_remaining()
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
        prewarmed: PrewarmedSession | None = prewarm_cached.pop(user.id, None)
        cache_hit = prewarmed is not None
        if prewarmed is None:
            pre_fut = prewarm_futures.pop(user.id, None)
            if pre_fut is not None and pre_fut.done():
                try:
                    prewarmed = pre_fut.result()
                except BookingBlockedError as e:
                    _mark_h2s_login_blocked(e)
                    prewarmed = None
                except Exception:
                    prewarmed = None
                if prewarmed:
                    prewarm_cache.set(user.id, prewarmed)
            elif pre_fut is not None:
                # 仍在运行中，放回 futures 让 _stash_pending_prewarms 收尾
                prewarm_futures[user.id] = pre_fut

        suppressed = _h2s_login_suppressed_remaining()
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

        sorted_cands = sorted(candidates, key=area_key, reverse=True)
        primary = sorted_cands[0]
        if len(sorted_cands) > 1:
            logger.info(
                "[%s] 自动预订候选 %d 套（含 %d 套备选），优先面积最大: %s (%.1f m²)",
                user.name, len(sorted_cands), len(sorted_cands) - 1,
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
            lambda cs=sorted_cands, u=user, pw=prewarmed:
            book_with_fallback(cs, u, booking_deadline, prewarmed=pw),
        )
        ab_futures.append((user, notifier, sorted_cands, f, prewarmed))

    return ab_futures


async def _notify_new_listings(
    new_listings: list["Listing"],
    user_notifiers: "UserNotifiers",
    web_notifier: "WebNotifier | None",
    storage: Storage,
    push,
) -> tuple[int, list]:
    """发送新房源通知（预订已在后台线程并行运行）。

    标记策略：任意用户通知成功即标记该 listing 为"已通知"。

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

            # APNs 推送钩子：现有渠道发送之后追加，独立 task，与其他渠道互不阻塞。
            user_round_matches.setdefault(user.id, []).append(listing)

        # Web 面板通知（每条新房源写一次，与用户过滤无关）
        if web_notifier:
            await web_notifier.send_new_listing(listing)

        if notified_this:
            new_notified_ids.append(listing.id)

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


async def _notify_status_changes(
    status_changes: list[tuple["Listing", str, str]],
    user_notifiers: "UserNotifiers",
    web_notifier: "WebNotifier | None",
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
        notified_this = False
        for user, notifier in user_notifiers:
            if not user.listing_filter.is_empty() and not user.listing_filter.passes(listing):
                logger.info("[%s] 状态变更跳过通知（过滤条件不符）: %s", user.name, listing.name)
                continue

            logger.info("[%s] 状态变更: %s  %s → %s", user.name, listing.name, old_status, new_status)
            ok = await notifier.send_status_change(listing, old_status, new_status)
            if ok:
                notified_this = True
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

        if notified_this:
            sc_notified_ids.append(listing.id)

    storage.mark_status_change_notified_batch(sc_notified_ids)
    return push_tasks


async def _process_booking_results(
    ab_futures: list[tuple],
    web_notifier: "WebNotifier | None",
    storage: Storage,
    push,
) -> list:
    """await 预订 Future，发送成功/失败通知，并聚合本轮屏蔽通知。

    Returns
    -------
    push_tasks: 屏蔽聚合时给 admin 推的 asyncio.Task（可能为空）。
    """
    push_tasks: list = []
    # 本轮被屏蔽的用户（含 notifier），所有候选 await 完后聚合发一条节流通知，
    # 避免每个用户/每个候选都发一次"预订失败"刷屏。
    blocked_in_round: list[tuple[UserConfig, BaseNotifier, str]] = []

    for user, notifier, sorted_cands, future, prewarmed in ab_futures:
        result = await future
        if result is None:
            continue
        # phase="blocked" 或 unknown_error 都意味着 session 可能已被 H2S 标记，
        # 失效 prewarm 缓存让下轮换新 session+token+TLS 指纹。
        if prewarmed and result.phase in ("unknown_error", "blocked"):
            prewarm_cache.invalidate(user.id)
            logger.info("[%s] 因 %s 失效 prewarm 缓存", user.name, result.phase)
        # result.listing 是实际被尝试预订的那套房源（fallback 后可能不是 sorted_cands[0]）
        booked_listing = result.listing

        # 更新重试队列（dry_run 不改变队列状态，避免污染正式运行时的数据）
        if not result.dry_run:
            if result.phase == "race_lost":
                retry_queue.add(user.id, {c.id for c in sorted_cands})
                logger.info(
                    "[%s] 📝 %d 套候选加入重试队列（下次扫描仍可预订时将重试）",
                    user.name, len(sorted_cands),
                )
            elif result.phase == "blocked":
                # blocked 是 IP/指纹级，不是房源级问题；保留 retry_queue 状态不动，
                # 等下轮换指纹后再决定是否重试。
                _mark_h2s_login_blocked(result.message)
                pass
            else:
                for c in sorted_cands:
                    retry_queue.discard(user.id, c.id)

        if result.dry_run:
            logger.info("[%s] [DRY RUN] 自动预订跳过: %s", user.name, booked_listing.name)
        elif result.phase == "blocked":
            # 不发 per-candidate booking_failed —— 累积到后面聚合一次性发
            blocked_in_round.append((user, notifier, result.message))
        elif result.success:
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
                await web_notifier.send_booking_failed(
                    booked_listing,
                    result.message,
                    user_id=user.id,
                )

    # ── 聚合屏蔽通知（共享 scrape 的 30 min 节流，避免双重打扰）────── #
    if blocked_in_round and _should_notify_block():
        names = sorted({u.name for u, _, _ in blocked_in_round})
        # 取第一条 msg 作为详情（所有候选的 message 通常相同 —— 都是同一 CF 屏蔽）
        detail = blocked_in_round[0][2]
        agg_msg = (
            f"🚫 自动预订被 403 屏蔽（{len(blocked_in_round)} 套候选 / "
            f"{len(names)} 个用户）\n\n{detail}\n\n"
            f"影响用户: {', '.join(names)}\n"
            f"30 分钟内不会重复通知。"
        )
        for u, n, _ in blocked_in_round:
            await n.send_error(agg_msg)
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
            len({u.id for u, _, _ in blocked_in_round}),
        )

    return push_tasks


async def run_once(
        cfg,
        storage: Storage,
        user_notifiers: UserNotifiers,
        *,
        web_notifier: WebNotifier | None = None,
        dry_run: bool = False,
        booking_deadline: float = float("inf"),
) -> dict[str, bool]:
    """
    执行一次完整的「抓取 → 对比 → 通知 → 自动预订」流程。

    Parameters
    ----------
    cfg              : 当前全局配置（Config 实例）
    storage          : SQLite 持久化层
    user_notifiers   : [(UserConfig, BaseNotifier), ...]，启用的用户列表
    dry_run          : True 时（--test 模式）只打印结果，不写库不发通知
    booking_deadline : time.monotonic() 截止时刻，传给 book_with_fallback()；
                       超过截止时不再尝试备选房源。
                       默认 float("inf") = 无限制（--once / --test 模式）。

    流程说明
    --------
    1. dispatch_scrape_tasks() 在 executor 线程中运行（同步 → 异步桥接）
    2. 记录城市完整扫描信号，并返回给 main_loop 的 stale 收敛逻辑
    3. storage.diff() 识别 new_listings 和 status_changes
    4. 快速候选预扫描（纯内存，无网络）：
       - 同时扫描 new_listings 和 status_changes，收集每个用户的自动预订候选
       - 无论来源（新上线 / 状态变更 → Available to book），立即提交 try_book()
         到线程池（run_in_executor），预订与通知并行执行
    5. 遍历 new_listings：发送新房源通知（预订已在后台运行）
    6. 遍历 status_changes：发送状态变更通知（预订已在后台运行）
    7. await 预订 Future，发送预订成功/失败通知
    8. 更新 meta（last_scrape_at / last_scrape_count）

    并行策略
    --------
    try_book() 是同步函数，通过 run_in_executor 在线程池中运行。
    所有候选在步骤 3 末尾立即提交，步骤 4/5 的通知网络调用（send_*）与之并行进行。
    到步骤 6 await 时，booking 往往已经完成，几乎零额外等待。
    预订请求在发出通知之前就已进入 Holland2Stay 服务器，可节省 1-3 秒。
    """
    scrape_tasks = cfg.scrape_tasks_v2()
    logger.info("开始抓取，任务数: %d，活跃用户数: %d", len(scrape_tasks), len(user_notifiers))

    if not scrape_tasks:
        logger.warning("未配置任何抓取任务，本轮不抓取。请检查 .env 中 SOURCES / CITIES / OURDOMAIN_CITIES 设置。")
        return {}

    # 分片**之前**的每 source 总 target 数。遥测要同时记「本轮抓了几个」和
    # 「一共配了几个」——健康判定靠两者是否相等来区分「这一片没房」和
    # 「整个 source 没房」（见 mcore/health.py 的 zero_streak 规则）。
    source_totals: dict[str, int] = {}
    for _t in scrape_tasks:
        source_totals[_t.source] = source_totals.get(_t.source, 0) + 1

    # 先按 source 节流（多久抓一次），再分片（每轮抓几个）——两者管的不是
    # 同一件事，顺序不能反：先分片会让被跳过的 source 白白推进分片游标。
    scrape_tasks = _apply_source_intervals(scrape_tasks, cfg, storage, dry_run=dry_run)
    scrape_tasks = _apply_task_sharding(scrape_tasks, cfg, storage, dry_run=dry_run)

    loop = asyncio.get_running_loop()

    # ── H2S source-level circuit breaker ─────────────────────────── #
    # H2S GraphQL 403 只应该暂停 H2S；OurDomain / Xior 等其它 source 仍可
    # 正常抓取、入库、通知。H2S 熔断期间完全跳过 H2S 任务；冷却到期后只放
    # 1 个城市做 canary，成功后下一轮再恢复完整 H2S 扫描。
    async def _dispatch_with_h2s_circuit(tasks):
        other_tasks, h2s_tasks = _split_h2s_tasks(tasks)
        selected_h2s, h2s_mode = _select_h2s_tasks_for_circuit(h2s_tasks)

        fresh_all: list[Listing] = []
        completeness_all: dict[str, bool] = {}
        h2s_blocked: BlockedError | None = None
        source_failures: list[tuple[str, Exception]] = []
        succeeded_sources: list[str] = []

        # 整轮是否跨多个 source。dispatcher 自己看不出来——它每次只收到一个
        # source 的任务，所以 completeness key 的 source 前缀得靠这个开关。
        # 用 tasks（本轮全部任务）而不是熔断后筛剩的：H2S 熔断期间前缀不该
        # 忽然消失，否则同一批 listing 的 key 形态会在两轮之间跳变。
        multi_source = len({t.source for t in tasks}) > 1

        # 整轮共用一个时间戳，各 source 的遥测行靠它分组。用 dispatch 开始的
        # 时刻而不是各 source 各自的完成时刻——否则同一轮的行会散成好几组。
        round_at = datetime.now(timezone.utc).isoformat()

        def _ckey(source: str, city_display: str) -> str:
            """与 ``scrapers._completeness_key`` 保持一致的 key 构造。"""
            return f"{source}:{city_display}" if multi_source else city_display

        async def _dispatch(selected, *, isolated: bool = False, browser_source: str = ""):
            result = await _dispatch_scrape_tasks_async(
                loop,
                selected,
                isolated=isolated,
                browser_source=browser_source,
                multi_source=multi_source,
            )
            return _unpack_scrape_result(result)

        async def _dispatch_isolated(src: str, group: list) -> None:
            """跑一个 source 的全部任务，失败只影响这个 source。

            浏览器型 source（H2S / Xior）必须绑到各自的长存单线程
            （见 ``_get_browser_executor``）：Playwright 的对象绑定创建线程，
            默认 executor 的线程不固定，跨线程调用会抛
            ``greenlet.error: Cannot switch to a different thread``。
            """
            started_at = time.monotonic()
            try:
                fresh_part, completeness_part = await _dispatch(
                    group,
                    isolated=src in _BROWSER_SOURCES,
                    browser_source=src,
                )
            except Exception as e:
                source_failures.append((src, e))
                # 标 ✗ 而不是留空：完整扫描那行日志要看得出「这个 source 塌了」，
                # 且 False 会正确挡住该城市的 stale 收敛。
                for t in group:
                    completeness_all.setdefault(_ckey(src, t.city_display), False)
                logger.error(
                    "source %s 整体抓取失败，已隔离该 source（%d 个任务）: %s: %s",
                    src, len(group), type(e).__name__, e,
                    exc_info=True,
                )
                if not dry_run:
                    _record_source_round(
                        storage, round_at=round_at, source=src,
                        targets=len(group), started_at=started_at, error=e,
                        total_targets=source_totals.get(src, len(group)),
                    )
                return
            succeeded_sources.append(src)
            fresh_all.extend(fresh_part)
            completeness_all.update(completeness_part)
            if not dry_run:
                complete_n, total_n = _completeness_stats(completeness_part)
                _record_source_round(
                    storage, round_at=round_at, source=src,
                    listings=len(fresh_part),
                    targets=total_n or len(group), complete=complete_n,
                    started_at=started_at,
                    total_targets=source_totals.get(src, len(group)),
                )

        # 逐 source 隔离。dispatcher 内部已经按 task 隔离，但它在「本次调用的
        # 任务全部失败」时仍会上抛——而 monitor 是按 source 分开调用它的，于是
        # 那个判定退化成了「单个 source 全失败」，等于没有跨 source 保护。
        #
        # 2026-08-03 实测：Xior 四栋楼连续 429 让 RateLimitError 逃出整个
        # dispatch，同轮 OurDomain 已抓到的结果被丢弃、H2S 排在后面根本没执行、
        # 39 个用户收到「监控将暂停 5 分钟」。24 小时内三次。
        for src in sorted({t.source for t in other_tasks}):
            await _dispatch_isolated(src, [t for t in other_tasks if t.source == src])

        if selected_h2s:
            started_at = time.monotonic()
            try:
                fresh_part, completeness_part = await _dispatch(
                    selected_h2s, isolated=True, browser_source=_H2S_SOURCE
                )
            except BlockedError as e:
                h2s_blocked = e
                _mark_h2s_scrape_blocked(e)
                if not dry_run:
                    _record_source_round(
                        storage, round_at=round_at, source=_H2S_SOURCE,
                        targets=len(selected_h2s), started_at=started_at, error=e,
                        total_targets=source_totals.get(_H2S_SOURCE, len(selected_h2s)),
                    )
            except Exception as e:
                # H2S 的非 Blocked 异常按旧契约照常上抛（熔断只管 403），
                # 但先留痕——否则遥测里 H2S 会在这类失败时凭空消失。
                if not dry_run:
                    _record_source_round(
                        storage, round_at=round_at, source=_H2S_SOURCE,
                        targets=len(selected_h2s), started_at=started_at, error=e,
                        total_targets=source_totals.get(_H2S_SOURCE, len(selected_h2s)),
                    )
                raise
            else:
                succeeded_sources.append(_H2S_SOURCE)
                fresh_all.extend(fresh_part)
                completeness_all.update(completeness_part)
                if h2s_mode == "canary":
                    _mark_h2s_scrape_recovered()
                if not dry_run:
                    complete_n, total_n = _completeness_stats(completeness_part)
                    _record_source_round(
                        storage, round_at=round_at, source=_H2S_SOURCE,
                        listings=len(fresh_part),
                        targets=total_n or len(selected_h2s), complete=complete_n,
                        started_at=started_at,
                        total_targets=source_totals.get(_H2S_SOURCE, len(selected_h2s)),
                    )
        elif h2s_tasks and h2s_mode == "open" and not dry_run:
            # 熔断期完全跳过 H2S。不记这一行的话，遥测里 H2S 会直接消失，面板
            # 只能看到 last_round_at 不断变老——与「进程挂了」无从区分。
            # 记成一次带 CircuitOpen 标记的失败，让「按设计退避」本身可见。
            _record_source_round(
                storage, round_at=round_at, source=_H2S_SOURCE,
                targets=len(h2s_tasks),
                total_targets=source_totals.get(_H2S_SOURCE, len(h2s_tasks)),
                error_type="CircuitOpen",
                error_msg=_h2s_circuit_reason or "circuit open",
            )

        # 一个都没成功 = 本轮没有任何可用数据，维持旧契约上抛，让 main_loop 走
        # 冷却而不是原速空转。h2s_blocked 不在此列——H2S 熔断本身就是退避，且
        # run_once 对它有专门的分支。
        if source_failures and not succeeded_sources and h2s_blocked is None:
            raise _pick_round_failure(source_failures)

        return fresh_all, completeness_all, h2s_blocked, h2s_mode

    # ── Candidate-only prewarm：只对本轮真实候选登录 ──────────────── #
    # 重要：prewarm 不能在 scrape 前启动，也不能每轮给所有自动预订用户刷新。
    # 当前策略是：先完成只读 scrape + diff，确认本轮确实有 H2S 可订候选后，
    # 只给被分配到候选的用户准备预登录；没有候选的轮次完全不碰 H2S 登录接口。
    prewarm_cached: dict[str, "PrewarmedSession"] = {}  # 命中：同步可用
    prewarm_futures: dict[str, "asyncio.Future"] = {}  # 未命中：后台刷新

    def _start_prewarm_for_candidates(candidate_user_ids: set[str]) -> None:
        """只为本轮实际有 H2S 自动预订候选的用户启动预登录。"""
        if dry_run:
            return
        suppressed = _h2s_login_suppressed_remaining()
        if suppressed > 0:
            logger.warning(
                "跳过 H2S prewarm：登录/预订 403 抑制窗口仍剩 %d 秒",
                suppressed,
            )
            return

        # 1) 失效不再合格的缓存（用户被禁用 / 移除自动预订 / 删除账号）
        active_user_ids = set()
        for user, _ in user_notifiers:
            ab = user.auto_book
            if ab.enabled and ab.email and ab.password:
                active_user_ids.add(user.id)
        for stale_uid in set(prewarm_cache.keys()) - active_user_ids:
            prewarm_cache.invalidate(stale_uid)

        if not candidate_user_ids:
            return

        # 2) 对合格用户：命中复用，未命中提交刷新。
        # 给每个 future 加 done_callback：完成后自动写入 prewarm_cache，
        # 确保慢 future（跨轮完成）的 session 不会泄漏。
        def _on_prewarm_done(user_id: str, fut) -> None:
            try:
                ps = fut.result()
            except BookingBlockedError as e:
                _mark_h2s_login_blocked(e)
                ps = None
            except Exception:
                ps = None
            if ps:
                prewarm_cache.set(user_id, ps)

        for user, _ in user_notifiers:
            if user.id not in active_user_ids:
                continue
            if user.id not in candidate_user_ids:
                continue
            cached = prewarm_cache.get(user.id)
            if prewarm_cache.is_valid(cached, user.auto_book.email):
                prewarm_cached[user.id] = cached
            else:
                if cached:
                    prewarm_cache.invalidate(user.id)
                fut = loop.run_in_executor(
                    None, prewarm_cache.create, user
                )
                prewarm_futures[user.id] = fut
                fut.add_done_callback(
                    lambda f, uid=user.id: loop.call_soon_threadsafe(
                        _on_prewarm_done, uid, f
                    )
                )

        if prewarm_cached or prewarm_futures:
            logger.debug(
                "prewarm 状态: 命中 %d / 刷新 %d",
                len(prewarm_cached), len(prewarm_futures),
            )

    async def _wait_for_candidate_prewarms() -> None:
        """候选轮次给 prewarm 最多 2 秒赶上快速通道，避免额外正常登录。"""
        if not prewarm_futures:
            return
        done, pending = await asyncio.wait(
            list(prewarm_futures.values()),
            timeout=_PREWARM_CANDIDATE_WAIT_SEC,
        )
        if done or pending:
            logger.debug(
                "candidate prewarm 等待 %.1fs：完成 %d / 未完成 %d",
                _PREWARM_CANDIDATE_WAIT_SEC,
                len(done),
                len(pending),
            )

    async def _stash_pending_prewarms() -> None:
        """收集已完成的 prewarm future，供本轮 booking 快速通道使用。
        未完成的 future 由 done_callback 兜底——完成后自动写入缓存。"""
        for user_id, fut in list(prewarm_futures.items()):
            if not fut.done():
                continue
            try:
                ps = fut.result()
            except BookingBlockedError as e:
                _mark_h2s_login_blocked(e)
                ps = None
            except Exception:
                ps = None
            if ps:
                prewarm_cache.set(user_id, ps)
            del prewarm_futures[user_id]

    # 注意：每个 except 路径显式调用 _stash_pending_prewarms()。
    # 未用 try/finally 统一收尾——因为成功路径上 stash 要延迟到
    # booking 快速通道之后（line ~530），提前 stash 会导致 booking
    # 代码无法从 prewarm_futures 中取出刚完成的 session。
    try:
        fresh, completeness, h2s_blocked, h2s_mode = await _dispatch_with_h2s_circuit(scrape_tasks)
        _log_scrape_completeness(completeness)
        if h2s_blocked is not None:
            proxy_on = bool(get_proxy_url())
            if not dry_run and _should_notify_block():
                cooldown = _h2s_circuit_remaining()
                err_msg = (
                    f"🚫 H2S 抓取被 403 屏蔽\n\n{h2s_blocked}\n\n"
                    f"代理状态: {'已启用' if proxy_on else '未启用'}\n"
                    f"H2S 已进入 source 熔断，约 {cooldown // 60} 分钟后只做一次 canary 探测；"
                    f"其他平台会继续监控。30 分钟内不会重复通知。"
                )
                await _broadcast_error(user_notifiers, web_notifier, err_msg)
            if (
                not dry_run
                and _h2s_circuit_fail_streak >= _H2S_LONG_BLOCK_STREAK
                and _should_notify_h2s_long_block()
            ):
                cooldown = _h2s_circuit_remaining()
                await _notify_admin_only(
                    storage,
                    web_notifier,
                    (
                        "H2S 长时间被 block，需要检查服务器\n\n"
                        f"连续 H2S 403: {_h2s_circuit_fail_streak} 次\n"
                        f"当前 H2S 熔断剩余: 约 {max(1, cooldown // 60)} 分钟\n"
                        f"最近错误: {h2s_blocked}\n\n"
                        "请检查服务器网络、代理出口、Webshare 状态和 H2S GraphQL 访问情况。"
                    ),
                    kind="blocked",
                )
            if not fresh:
                logger.warning(
                    "H2S %s 触发 403 且没有其它 source 成功，本轮不更新数据库。",
                    h2s_mode,
                )
                return completeness
        elif h2s_mode == "open" and not fresh:
            # 条件是 `not fresh`（本轮一条房源都没抓到），不是「没有配置其它 source
            # 的任务」——原文案写成后者，会把人引去查 SOURCES 配置，而真实原因通常
            # 是其它 source 确实各自返回了 0 条。Xior 分轮抓之后这尤其常见：某一片
            # 正好全是没库存的楼，整轮合计就是 0。
            logger.warning(
                "H2S 熔断中，且其它 source 本轮未抓到任何房源，无数据可入库。"
            )
            return completeness
    except BlockedError as e:
        # 403 = Cloudflare WAF 屏蔽，等待无法恢复，必须换代理/重启。
        # 给 main_loop 一个长 cooldown（15 min），并节流通知避免刷屏。
        await _stash_pending_prewarms()
        proxy_on = bool(get_proxy_url())
        logger.error(
            "🚫 抓取被屏蔽 (HTTP 403) cities=%d users=%d proxy=%s: %s",
            len(scrape_tasks), len(user_notifiers),
            "yes" if proxy_on else "no", e,
        )
        if not dry_run and _should_notify_block():
            err_msg = (
                f"🚫 抓取被 403 屏蔽\n\n{e}\n\n"
                f"代理状态: {'已启用' if proxy_on else '未启用'}\n"
                f"30 分钟内不会重复通知。"
            )
            await _broadcast_error(user_notifiers, web_notifier, err_msg)
        raise
    except RateLimitError as e:
        # 429 需要更长冷却，上传给 main_loop 单独处理（不走普通 10s 恢复路径）
        await _stash_pending_prewarms()
        logger.warning(
            "⚠️  抓取被限流 cities=%d users=%d proxy=%s: %s",
            len(scrape_tasks), len(user_notifiers),
            "yes" if get_proxy_url() else "no",
            e,
        )
        if not dry_run:
            err_msg = f"⚠️ 抓取被限流（429）\n{e}\n监控将暂停 5 分钟后继续。"
            await _broadcast_error(user_notifiers, web_notifier, err_msg)
        raise
    except UpstreamMaintenanceError as e:
        # 平台维护：自己会恢复，**不给普通用户**发告警（用户什么也做不了），
        # 但是给 **admin 的 web 通知面板** 发一条（节流 1 小时一次）——admin
        # 能从中看到维护开始时间，方便日后排查"那段时间为什么没数据"。
        #
        # 持久化状态用两个 meta key 驱动 dashboard banner：
        #   - upstream_maintenance_seen_at：首次探测到维护的时间（持续期间不刷新）
        #   - upstream_maintenance_last_at：最近一次仍在维护的时间（每次都刷新）
        # 等再次抓取成功时清空（在成功路径里做）。
        await _stash_pending_prewarms()
        logger.info(
            "🔧 H2S 平台维护中，本轮跳过 cities=%d users=%d: %s",
            len(scrape_tasks), len(user_notifiers), e,
        )
        if not dry_run:
            now_iso = datetime.now(timezone.utc).isoformat()
            first_detect = not storage.get_meta("upstream_maintenance_seen_at", default="")
            if first_detect:
                storage.set_meta("upstream_maintenance_seen_at", now_iso)
            storage.set_meta("upstream_maintenance_last_at", now_iso)

            # admin web 通知：首次探测 + 1h 节流后再次复查命中时才发，避免每轮刷屏。
            # 不走 user_notifiers——他们的 push / iMessage 不该在凌晨被维护吵醒。
            if web_notifier and _should_notify_maintenance():
                hint = (
                    "🔧 H2S 平台计划维护中\n\n"
                    f"{e}\n\n"
                    "监控已暂停轮询，平台恢复后会自动继续。"
                    "无需操作；状态会在 dashboard 顶部 banner 显示。"
                )
                await web_notifier.send_error(hint)
        raise
    except ProxyError as e:
        # 抓取代理失效（HTTPS_PROXY 502 / 隧道失败）。ProxyError 是
        # ScrapeNetworkError 子类——控制流仍走网络失败冷却，但：
        # 1. 把失败的代理标记进 cooldown → 下一轮 get_proxy_url 自动切到备用
        #    （SCRAPE_PROXIES_FALLBACK 配了的话）
        # 2. 给 admin 发一条明确告警（30 min 节流）。不发普通用户——改不了代理
        from config import is_proxy_in_cooldown, proxy_failure_mark_count, report_proxy_failure
        await _stash_pending_prewarms()
        old_proxy = get_proxy_url()
        service_error = is_proxy_service_error(e)
        new_proxy = report_proxy_failure(service_error_confirmed=service_error)
        confirmed_down = is_proxy_in_cooldown(old_proxy)
        native_fallback = is_proxy_native_fallback_active()
        switched_proxy = bool(new_proxy and new_proxy != old_proxy)
        logger.error(
            "🛰️ 抓取代理故障%s: %s",
            (
                "，下一轮切换到备用代理"
                if switched_proxy
                else "，下一轮降级为服务器原生 IP 直连"
                if native_fallback
                else f"，服务端异常待确认（{proxy_failure_mark_count(old_proxy)}/2）"
                if old_proxy and not confirmed_down and service_error
                else "，未确认代理服务端异常，按普通网络失败处理"
                if old_proxy and not service_error
                else "（无备用代理可切）"
            ),
            e,
        )
        # admin 告警只在「已确认且可操作」时发，避免一次瞬时失败就刷消息：
        #   - switched_proxy   切到了备用代理
        #   - native_fallback  全部代理冷却、降级直连原生 IP
        #   - not old_proxy    根本没代理可用、彻底卡住
        # 未达连续确认阈值的单次失败、或非代理服务端特征的普通网络抖动 →
        # 只记日志，不打扰 admin（由 main_loop 的网络失败计数兜底）。
        should_alert = switched_proxy or native_fallback or not old_proxy
        if not dry_run and should_alert and _should_notify_proxy():
            if switched_proxy:
                tail = "已自动切换到备用代理，下一轮重试。请尽快修复主代理。"
            elif native_fallback:
                tail = (
                    "所有已配置代理都在冷却中，监控将临时降级为服务器原生 IP 直连。"
                    "降级期间抓取频率最多 10 分钟一次，代理冷却结束后会自动恢复。"
                )
            else:
                tail = (
                    "监控已暂停抓取（代理不通时无法绕过 Cloudflare）。"
                    "请检查 HTTPS_PROXY，或配置 SCRAPE_PROXIES_FALLBACK 备用代理。"
                )
            msg = f"抓取代理失效\n\n{e}\n\n{tail}\n30 分钟内不重复通知。"
            if web_notifier:
                await web_notifier.send_error(msg)
            from mcore import push as _push
            try:
                await _push.dispatch_admin(storage, msg, kind="proxy")
            except Exception:
                logger.debug("代理失效 admin push 失败（已忽略）", exc_info=True)
        raise
    except ScrapeNetworkError as e:
        # 全部城市第 1 页网络失败 → 不更新 last_scrape_at（非有效抓取），
        # 上传让 main_loop 做连续失败计数和冷却。不发给用户通知——
        # 网络抖动通常几轮后自动恢复，连续失败到阈值才告警。
        await _stash_pending_prewarms()
        logger.error(
            "抓取全部网络失败 cities=%s users=%d proxy=%s: %s",
            _task_labels(scrape_tasks), len(user_notifiers),
            "yes" if get_proxy_url() else "no",
            e,
        )
        raise
    except Exception as e:
        # 抓取阶段未被归类的异常（不属于 Blocked/RateLimit/Maintenance/Proxy/
        # Network）。普通用户对内部异常无能为力——改为只告警 admin 并加 30 min
        # 节流，避免某个反复抛错的内部 bug 每轮刷屏所有用户渠道。
        await _stash_pending_prewarms()
        logger.error(
            "抓取阶段未分类错误 cities=%s users=%d: %s",
            _task_labels(scrape_tasks), len(user_notifiers), e,
            exc_info=True,
        )
        if not dry_run and _should_notify_internal():
            await _notify_admin_only(
                storage, web_notifier,
                f"抓取阶段未分类错误\n\n{type(e).__name__}: {e}\n\n"
                f"这是一条未被归类的内部异常，请查看服务器日志排查。"
                f"30 分钟内不重复通知。",
                kind="error",
            )
        return {}

    logger.info("本次抓取共 %d 条房源", len(fresh))

    if dry_run:
        _print_dry_run(fresh, user_notifiers)
        return completeness

    # ── 抓取后管线：对比入库 → 预订 → 通知 → 推送 ─────────────────── #
    # 这段过去裸跑在 run_once 顶层，没有任何 except——一旦 storage.diff() /
    # set_meta() / 通知分发抛异常（DB 锁死、磁盘满、schema 损坏等），会一路
    # 冒泡到 main_loop 的 generic except，只打日志、不通知任何人，等于监控
    # 静默瘫痪。这里统一兜底：归类为"数据/通知管线错误"，给 admin 发一条
    # 带类型的告警（30 min 节流），然后 return {} 走正常间隔——避免 re-raise
    # 触发 main_loop 的 10s 紧重试反复重新抓站。
    try:
        new_listings, status_changes = storage.diff(fresh)

        # ── 影子 source：入库了，但不通知 ─────────────────────────────── #
        # 新平台上线前的静默验证期：先确认它抓得对、数据长什么样，再决定是否
        # 对用户开放。**必须在 diff() 之后过滤**——diff 要照常执行，房源才会
        # 进库、状态变更才会被记录；被拦掉的只有「告诉谁」这一步。
        #
        # 副作用是这些 listing 的 notified 一直是 0。取消影子后不会补发历史
        # ——diff() 只对真正的新 id 产出 new_listings，老的不会再冒出来。
        # 这是想要的：解除影子不该给用户灌一堆积压通知。
        new_listings, status_changes = _drop_shadow_sources(
            cfg, new_listings, status_changes,
        )

        # diff() 成功后再写时间戳，确保面板显示的 last_scrape_at 对应一次完整的
        # "抓取 + 入库" 操作；若 diff() 抛异常，时间戳不会被更新。
        # 计数用 fresh（含影子 source）——它回答的是「抓到多少」，不是「通知了多少」。
        storage.set_meta("last_scrape_at", datetime.now(timezone.utc).isoformat())
        storage.set_meta("last_scrape_count", str(len(fresh)))

        # 维护态恢复：本轮成功就清掉 maintenance meta，让 dashboard banner 消失。
        _clear_maintenance_meta_if_recovered(storage)

        # ── 快速候选预扫描：纯内存收集候选，抢在发通知之前提交预订 ──────── #
        ab_candidates, status_transition = _collect_booking_candidates(
            new_listings, status_changes, fresh, user_notifiers,
        )
        candidate_user_ids = {
            uid for uid, candidates in ab_candidates.items() if candidates
        }
        _start_prewarm_for_candidates(candidate_user_ids)
        await _wait_for_candidate_prewarms()

        # ── 立即将 book_with_fallback() 提交到线程池（快速通道）──────── #
        # 新上线可预订 / 状态变更 → Available to book 均立即提交 run_in_executor，
        # 预订请求在发出通知之前就进入 Holland2Stay 服务器（节省 1-3 秒）。
        ab_futures = _submit_bookings(
            loop, ab_candidates, user_notifiers,
            prewarm_cached, prewarm_futures, status_transition, booking_deadline,
        )

        # 没有候选的用户的 prewarm（如果是新刷新的）存入缓存供下轮复用
        await _stash_pending_prewarms()

        # ── 新房源通知（预订已在后台线程并行运行）───────────────────── #
        # 标记策略：任意用户通知成功即标记为"已通知"。若部分用户渠道失败，
        # 该 listing 不会补发——实际业务中多用户同渠道很少部分失败。
        # APNs 推送：与现有 4 渠道并行，fire-and-forget；
        # 同一用户本轮匹配 ≥ push.aggregate_threshold() 套时改聚合一条。
        from mcore import push as _push  # 局部 import，避免冷启动加载 httpx

        total_notified, new_push = await _notify_new_listings(
            new_listings, user_notifiers, web_notifier, storage, _push,
        )
        sc_push = await _notify_status_changes(
            status_changes, user_notifiers, web_notifier, storage, _push,
        )
        booking_push = await _process_booking_results(
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
        retry_queue.save(storage)

        logger.info(
            "本轮结束: %d 新房源（已通知 %d），%d 状态变更，数据库共 %d 条",
            len(new_listings), total_notified, len(status_changes), storage.count_all(),
        )
        return completeness
    except Exception as e:
        # 抓取已成功、但入库/通知管线挂了。给 admin 一条带类型的告警，return {}
        # 走正常间隔，避免对已抓到的数据做无意义的 10s 紧重试。
        await _stash_pending_prewarms()
        logger.error(
            "抓取后管线错误（入库/通知）listings=%d: %s",
            len(fresh), e, exc_info=True,
        )
        if _should_notify_internal():
            await _notify_admin_only(
                storage, web_notifier,
                f"数据/通知管线错误\n\n{type(e).__name__}: {e}\n\n"
                f"房源已抓到 {len(fresh)} 条，但写库或发通知阶段失败"
                f"（如数据库锁死/磁盘满）。请尽快查看服务器日志与磁盘。"
                f"30 分钟内不重复通知。",
                kind="error",
            )
        return {}


def _apns_startup_diag(st, users: list[UserConfig]) -> None:
    """启动时诊断 APNs 设备关联，发现配置问题尽早 WARNING。"""
    from mcore import push as _push

    if _push.get_client() is None:
        logger.warning("APNs 未启用：启动时无法获取 ApnsClient（检查 APNS_ENABLED / .p8 / APNS_*）")
        return

    # 统计全局数据
    try:
        total_devs = st.conn.execute(
            "SELECT COUNT(*) FROM device_tokens WHERE disabled_at IS NULL"
        ).fetchone()
        total_tokens = st.conn.execute(
            "SELECT COUNT(*) FROM app_tokens WHERE revoked = 0"
        ).fetchone()
        logger.info(
            "APNs 启动诊断：DB 中 %d 个活跃设备，%d 个活跃 token",
            total_devs[0] if total_devs else 0,
            total_tokens[0] if total_tokens else 0,
        )
    except Exception:
        logger.exception("APNs 启动诊断：查询设备/ token 计数失败")
        return

    # admin 设备诊断
    try:
        admin_devs = st.get_active_devices_for_admin()
        if admin_devs:
            logger.info("APNs admin: %d 个可推送设备", len(admin_devs))
        else:
            admin_tokens = st.conn.execute(
                "SELECT COUNT(*) FROM app_tokens WHERE role='admin' AND user_id IS NULL AND revoked=0"
            ).fetchone()
            logger.warning(
                "APNs admin: 没有可推送设备！（%s活跃 admin token）",
                "有" if (admin_tokens and admin_tokens[0] > 0) else "无",
            )
    except Exception:
        logger.exception("APNs 启动诊断：get_active_devices_for_admin 失败")

    # 逐用户查关联
    for u in users:
        if not u.enabled:
            continue
        try:
            devs = st.get_active_devices_for_user(u.id)
        except Exception:
            logger.exception("APNs 启动诊断：get_active_devices_for_user 失败 user=%s", u.name)
            continue
        if devs:
            logger.info(
                "APNs 用户 %s (id=%s): %d 个可推送设备",
                u.name, u.id, len(devs),
            )
        else:
            # 查一下这个 user_id 有没有 token
            token_count = st.conn.execute(
                "SELECT COUNT(*) FROM app_tokens WHERE user_id = ? AND revoked = 0",
                (u.id,),
            ).fetchone()
            has_tokens = token_count and token_count[0] > 0
            logger.warning(
                "APNs 用户 %s (id=%s): 没有可推送设备！（该 user %s活跃 token）"
                "—— 请确认 iOS App 已用此账号登录并在 Settings 中注册了设备",
                u.name, u.id,
                "有" if has_tokens else "无",
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
        2. 独立执行 stale listing 状态收敛
        3. 按 heartbeat_interval_minutes 间隔发心跳
        4. asyncio.wait_for(_reload_event, timeout=actual_interval)
           - 超时：正常进入下一轮
           - 事件触发（SIGHUP）：热重载 cfg + users，重建 user_notifiers
        5. 未预期异常：记录并 sleep 10s，不退出进程

    热重载
    ------
    SIGHUP 信号处理器通过 loop.call_soon_threadsafe 设置 _reload_event，
    使 wait_for 提前返回。热重载完成后清除事件，继续下一轮。
    """
    global _reload_event
    _reload_event = asyncio.Event()

    round_count = 0
    last_heartbeat_time = time.monotonic()  # 启动时记为刚发过，避免第一轮立即心跳
    last_stale_sweep_time = 0.0  # 启动后第一轮成功抓取即执行一次状态收敛
    stale_sweep_interval_sec = 24 * 60 * 60

    # 自适应高峰间隔：从 peak_interval 出发，成功则缩短，限流则翻倍退避。
    # 非高峰时重置，确保下次高峰期从 peak_interval 重新开始探测。
    adaptive_peak: float = float(cfg.peak_interval)

    logger.info(
        "监控启动，常规间隔 %d 秒，高峰期自适应 %d–%d 秒（%s–%s / %s–%s 荷兰时间），城市: %s，用户: %d 个",
        cfg.check_interval, cfg.min_interval, cfg.peak_interval,
        cfg.peak_start, cfg.peak_end, cfg.peak_start_2, cfg.peak_end_2,
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

    network_fail_streak = 0  # ScrapeNetworkError 连续计数
    blocked_fail_streak = 0  # Cloudflare 403 连续计数

    while True:
        round_count += 1
        try:
            base_interval, is_peak = get_interval(cfg)

            if is_peak:
                # 高峰期：使用自适应间隔，在 [min_interval, peak_interval] 范围内浮动
                effective_interval = max(cfg.min_interval, int(adaptive_peak))
                peak_tag = f"【高峰期 {effective_interval}s】"
            else:
                # 非高峰期：使用常规间隔，同时重置自适应（为下次高峰期做准备）
                effective_interval = base_interval
                adaptive_peak = float(cfg.peak_interval)
                peak_tag = ""

            native_fallback_active = is_proxy_native_fallback_active()
            if native_fallback_active:
                prev_interval = effective_interval
                effective_interval = max(effective_interval, _NATIVE_PROXY_FALLBACK_INTERVAL)
                if effective_interval != prev_interval:
                    logger.warning(
                        "🛰️ 代理全冷却，降级为服务器原生 IP 直连；本轮间隔限制为 %d 秒（原 %d 秒）",
                        effective_interval,
                        prev_interval,
                    )
                peak_tag = f"{peak_tag}【代理降级直连】"

            logger.info("===== 第 %d 轮 %s=====", round_count, peak_tag)

            # 心跳：**在抓取之前**写，抓取成功与否都刷新。
            #
            # 它回答的是「监控循环还活着吗」，和 last_scrape_at 的「抓到数据了
            # 吗」是两个问题。H2S 熔断冷却最长 6 小时（_H2S_CIRCUIT_MAX_COOLDOWN），那期间没有任何成功抓取，
            # 但 monitor 完全健康、只是在按设计退避——拿 last_scrape_at 做健康
            # 判定会把正常退避误报成故障。
            try:
                storage.set_meta(
                    "monitor_heartbeat_at",
                    datetime.now(timezone.utc).isoformat(),
                )
            except Exception:
                logger.debug("写 monitor 心跳失败（已忽略）", exc_info=True)

            # booking_deadline：在此时刻后不再尝试备选房源，让下一轮扫描优先进行
            booking_deadline = time.monotonic() + effective_interval
            city_completeness = await run_once(
                cfg,
                storage,
                user_notifiers,
                web_notifier=web_notifier,
                booking_deadline=booking_deadline,
            )

            # 记录本小时存活样本（用于 dashboard 7 天 uptime%）。幂等、持久，
            # 跟 listings 同库 → 同 Docker volume，重启/重建不丢，真实反映宕机。
            try:
                storage.record_uptime_sample()
            except Exception:
                logger.debug("record_uptime_sample 失败（已忽略）", exc_info=True)

            # 轮次遥测的保留期剪枝。方法自带每小时一次的节流，这里每轮直接调。
            storage.prune_round_stats()

            # 数据退化告警。和上面那些「对异常触发」的告警是互补的：这里看的是
            # 没有异常的故障——一直"成功"返回 0 条、完整率悄悄下滑。
            await _dispatch_watchdog_alerts(storage, web_notifier)

            # 成功：重置网络失败连续计数
            if network_fail_streak:
                logger.info("网络恢复，连续失败计数已清零（之前 %d 次）", network_fail_streak)
                network_fail_streak = 0
            if blocked_fail_streak:
                logger.info("Cloudflare 403 已恢复，连续屏蔽计数已清零（之前 %d 次）", blocked_fail_streak)
                blocked_fail_streak = 0

            # 成功：高峰期将自适应间隔缩短 5%（逐步逼近 min_interval）
            if is_peak:
                prev = adaptive_peak
                adaptive_peak = max(float(cfg.min_interval), adaptive_peak * _ADAPTIVE_DECREASE)
                if int(prev) != int(adaptive_peak):
                    logger.info(
                        "🔽 自适应间隔: %d → %d 秒（下限 %d 秒）",
                        int(prev), int(adaptive_peak), cfg.min_interval,
                    )

            # 状态收敛兜底：仅对本轮完整扫描成功的城市执行。
            # 整轮连接失败会在 run_once() 抛出并跳过这里；部分城市不完整则不收敛该城市。
            sweep = _stale_sweep_decision(
                city_completeness,
                last_stale_sweep_time,
                stale_sweep_interval_sec,
                now=time.monotonic(),
            )
            if sweep == "defer":
                logger.info(
                    "本轮无完整扫描城市，stale 收敛推迟到下一轮（不重置 24h 计时器）"
                )
            elif sweep == "run":
                try:
                    stale = _mark_stale_listings_for_complete_cities(
                        storage,
                        city_completeness,
                        days=7,
                        lottery_days=2,
                        monitored_pairs=_monitored_pairs(cfg),
                        orphan_days=30,
                    )
                    if stale:
                        logger.info(
                            "已将 %d 条未见 listing 推测为 Occupied"
                            "（在监控城市 book 7 天 / lottery 2 天；已移出监控的城市 30 天）",
                            stale,
                        )
                except Exception:
                    logger.exception("mark_stale_listings 失败（已忽略）")
                finally:
                    last_stale_sweep_time = time.monotonic()

            heartbeat_interval_sec = cfg.heartbeat_interval_minutes * 60
            if heartbeat_interval_sec > 0 and time.monotonic() - last_heartbeat_time >= heartbeat_interval_sec:
                total = storage.count_all()
                for _, notifier in user_notifiers:
                    await notifier.send_heartbeat(total_in_db=total, round_count=round_count)
                if web_notifier:
                    await web_notifier.send_heartbeat(total_in_db=total, round_count=round_count)
                # 清理旧通知，防止 web_notifications 表无限增长
                pruned = storage.prune_notifications(keep=500)
                if pruned:
                    logger.debug("已清理 %d 条旧通知", pruned)
                # 清理过期验证 token（保留 30 天审计窗口）
                try:
                    pruned_tok = storage.prune_expired_verifications()
                    if pruned_tok:
                        logger.debug("已清理 %d 条过期验证 token", pruned_tok)
                except Exception:
                    logger.exception("prune_expired_verifications 失败（已忽略）")
                # 清理超期 Resend 配额计数行（保留 30 天）
                try:
                    pruned_cnt = storage.prune_old_email_send_counters(keep_days=30)
                    if pruned_cnt:
                        logger.debug("已清理 %d 条旧配额计数", pruned_cnt)
                except Exception:
                    logger.exception("prune_old_email_send_counters 失败（已忽略）")
                last_heartbeat_time = time.monotonic()

            actual = apply_jitter(effective_interval, cfg.jitter_ratio)
            if native_fallback_active:
                actual = max(actual, _NATIVE_PROXY_FALLBACK_INTERVAL)
            dev_pct = (actual - effective_interval) / effective_interval * 100
            logger.info(
                "等待 %d 秒（基准 %d s，%+.0f%%）%s",
                actual, effective_interval, dev_pct,
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
                    # 用户可能改了密码/邮箱/账号 → 全量失效 prewarm 缓存。
                    # 下一轮 run_once 会按需重建（命中策略已对齐 active_user_ids）。
                    prewarm_cache.clear()
                    user_notifiers = _build_user_notifiers(users)
                    # 热重载后重置自适应间隔（用户可能改了 peak_interval / min_interval）
                    adaptive_peak = float(cfg.peak_interval)
                    logger.info(
                        "配置已热重载：城市=%s  用户=%d  间隔=%ds  高峰自适应=%d–%ds(%s–%s/%s–%s)",
                        [c.name for c in cfg.cities], len(user_notifiers),
                        cfg.check_interval, cfg.min_interval, cfg.peak_interval,
                        cfg.peak_start, cfg.peak_end, cfg.peak_start_2, cfg.peak_end_2,
                    )
                except Exception as e:
                    logger.error(
                        "热重载失败，继续使用旧配置: %s",
                        e, exc_info=True,
                    )

        except asyncio.CancelledError:
            raise  # 允许正常关闭（KeyboardInterrupt 等）
        except RateLimitError:
            # 被限流：自适应间隔翻倍退避，然后冷却 5 分钟
            prev = adaptive_peak
            adaptive_peak = min(float(cfg.check_interval), adaptive_peak * _ADAPTIVE_INCREASE)
            cooldown = apply_jitter(300, cfg.jitter_ratio)
            logger.warning(
                "⚠️  触发限流，自适应间隔 %d → %d 秒，冷却 %d 秒后继续",
                int(prev), int(adaptive_peak), cooldown,
            )
            await asyncio.sleep(cooldown)
        except BlockedError:
            # 被 Cloudflare 屏蔽：等待无法恢复，但仍然冷却 15 分钟以减少
            # 错误日志刷屏。连续命中时指数退避到最多 2 小时，避免同一出口
            # 每 15 分钟反复撞 GraphQL 把 WAF 状态越打越热。
            blocked_fail_streak += 1
            base = min(
                _BLOCKED_COOLDOWN_MAX,
                _BLOCKED_COOLDOWN * (2 ** max(0, blocked_fail_streak - 1)),
            )
            cooldown = apply_jitter(base, cfg.jitter_ratio)
            logger.error(
                "🚫 被 Cloudflare 屏蔽（连续 %d 次），冷却 %d 秒后再试。"
                "持续屏蔽请考虑：换 HTTPS_PROXY 出口 / 暂停几小时。",
                blocked_fail_streak, cooldown,
            )
            await asyncio.sleep(cooldown)
        except UpstreamMaintenanceError:
            # 平台维护：和 BlockedError 用相同冷却长度，但语义完全不同：
            #   - 不打 ERROR 日志（INFO 已在 run_once 里打过）
            #   - 不重置 adaptive_peak（恢复后立刻按正常节奏跑）
            #   - 不计入 network_fail_streak（不是网络问题）
            # 冷却结束后下一轮 run_once 会再次尝试，若仍维护则继续走本分支。
            cooldown = apply_jitter(_MAINTENANCE_COOLDOWN, cfg.jitter_ratio)
            logger.info(
                "🔧 H2S 平台维护中，冷却 %d 秒后再探。无需人工操作。",
                cooldown,
            )
            await asyncio.sleep(cooldown)
        except ProxyError as e:
            # 代理失效：run_once 已把坏代理标记冷却，下一轮 get_proxy_url 会切到
            # 备用；若所有代理都在冷却，则临时直连原生 IP，并把频率压到 10 min。
            from config import proxy_failure_mark_count, proxy_pool_size
            active_proxy = get_proxy_url()
            if is_proxy_native_fallback_active():
                cooldown = max(
                    apply_jitter(_NATIVE_PROXY_FALLBACK_INTERVAL, cfg.jitter_ratio),
                    _NATIVE_PROXY_FALLBACK_INTERVAL,
                )
                logger.warning(
                    "🛰️ 代理失效且全部代理在冷却，降级直连原生 IP；%d 秒后再试: %s",
                    cooldown,
                    e,
                )
                await asyncio.sleep(cooldown)
            elif active_proxy and proxy_failure_mark_count(active_proxy) > 0:
                cooldown = apply_jitter(_PROXY_CONFIRM_RETRY_DELAY, cfg.jitter_ratio)
                logger.warning(
                    "🛰️ 代理故障尚未确认，%d 秒后继续使用当前代理复核: %s",
                    cooldown,
                    e,
                )
                await asyncio.sleep(cooldown)
            elif proxy_pool_size() > 1:
                cooldown = apply_jitter(20, cfg.jitter_ratio)
                logger.warning(
                    "🛰️ 代理失效，%d 秒后用备用代理重试: %s", cooldown, e,
                )
                await asyncio.sleep(cooldown)
            else:
                # 无备用——和普通网络失败一样攒计数 + 长冷却
                network_fail_streak += 1
                cooldown = apply_jitter(_NETWORK_FAIL_COOLDOWN, cfg.jitter_ratio)
                logger.error("🛰️ 代理失效且无备用，冷却 %d 秒: %s", cooldown, e)
                await asyncio.sleep(cooldown)
        except ScrapeNetworkError as e:
            network_fail_streak += 1
            if network_fail_streak >= _NETWORK_FAIL_THRESHOLD:
                cooldown = apply_jitter(_NETWORK_FAIL_COOLDOWN, cfg.jitter_ratio)
                logger.error(
                    "🌐 连续 %d 次网络失败（阈值 %d），冷却 %d 秒。"
                    "每轮所有城市第 1 页均无法连接 — 请检查代理/网络。"
                    "最近错误: %s",
                    network_fail_streak, _NETWORK_FAIL_THRESHOLD, cooldown, e,
                )
                await asyncio.sleep(cooldown)
            else:
                logger.warning(
                    "🌐 网络失败 %d/%d（阈值 %d）: %s",
                    network_fail_streak, _NETWORK_FAIL_THRESHOLD, _NETWORK_FAIL_THRESHOLD, e,
                )
                await asyncio.sleep(10)
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

    # 强制从 .env 文件重新加载（override=True 覆盖继承的环境变量），
    # 确保子进程启动时使用最新的 .env 配置而非父进程的陈旧值。
    load_dotenv(dotenv_path=ENV_PATH, override=True)
    cfg = load_config()
    _setup_logging(cfg.log_level)

    if not args.test:
        check_for_updates()

    if args.test:
        logger.info("TEST 模式：只抓取，不发通知")
        scrape_tasks = cfg.scrape_tasks_v2()
        scrape_result = await asyncio.get_running_loop().run_in_executor(
            None, lambda: dispatch_scrape_tasks(scrape_tasks)
        )
        fresh, completeness = _unpack_scrape_result(scrape_result)
        _log_scrape_completeness(completeness)
        print(json.dumps([l.to_dict() for l in fresh], ensure_ascii=False, indent=2))
        return

    # ── 数据库重置 ────────────────────────────────────────────────── #
    if args.reset_db:
        db = Storage(cfg.db_path, timezone_str=cfg.timezone)
        db.reset_all()
        db.close()
        logger.warning("数据库已清空，所有历史记录已删除")

    storage = Storage(cfg.db_path, timezone_str=cfg.timezone)

    # dashboard 7 天 uptime% 改用"每小时存活采样"（见 Storage.record_uptime_sample），
    # 抗重启、真实反映宕机。旧的 monitor_started_at 单时间戳方案已弃用——它超 7 天
    # 后下次重启会被覆盖回 now → 掉到 1%，且不感知中途宕机。

    # 启动即记一个存活样本，避免刚拉起时 dashboard 短暂显示 0%
    try:
        storage.record_uptime_sample()
    except Exception:
        logger.debug("启动 record_uptime_sample 失败（已忽略）", exc_info=True)

    # 恢复持久化的竞败重试队列（进程重启后不丢失）
    retry_queue.load(storage)

    # 加载用户配置；旧 users.json 迁移损坏时硬停止，避免忽略或覆盖现有数据
    try:
        users = load_users()
    except RuntimeError as e:
        logger.critical("❌ 无法加载用户配置，进程终止以防数据丢失:\n  %s", e)
        sys.exit(1)

    if not users:
        logger.warning(
            "⚠️  当前没有用户配置，通知和自动预订不可用。"
            "请在 Web 面板点击「新增用户」添加用户。"
        )

    user_notifiers = _build_user_notifiers(users)
    if not user_notifiers:
        logger.warning("没有启用的用户，通知功能不可用（监控仍会写库）")

    # Web 面板通知：与平台无关，始终创建
    web_notifier = WebNotifier(storage)
    logger.info("Web 面板通知已启用（所有事件将写入 web_notifications 表）")

    # APNs 诊断：启动时校验每个 user 的设备关联，避免静默丢通知
    _apns_startup_diag(storage, users)

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
        prewarm_cache.clear()
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
