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

from env_utils import env_int


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

from config import (DATA_DIR, ENV_PATH, get_proxy_url, is_personal_proxy_active,
                    is_proxy_native_fallback_active, load_config)
from models import Listing
from notifier import BaseNotifier, WebNotifier, create_user_notifier
from mcore.health import CIRCUIT_OPEN_ERROR  # noqa: F401  （测试从这里取）
from mcore.state import (
    BLOCK_NOTIFY_INTERVAL as _BLOCK_NOTIFY_INTERVAL,  # noqa: F401
    retry_queue,
    runtime,
)
from mcore.throttles import (
    should_notify_block as _should_notify_block,  # noqa: F401
)
from mcore.h2s_guard import (
    H2S_SOURCE,
    h2s_login_suppressed_remaining as _h2s_login_suppressed_remaining,  # noqa: F401
)
from mcore.interval import apply_jitter, get_interval
from mcore.prewarm_round import PrewarmRound
from mcore.post_round import run_post_scrape as _run_post_scrape
from mcore.round_failures import (
    handle_h2s_blocked as _handle_h2s_blocked,
    handle_scrape_failure as _handle_scrape_failure,
)
from mcore.h2s_rest import RestTrigger
from mcore import geocode as _geocode
# 从 monitor.py 拆出的自包含块。保留带下划线的别名，测试与既有调用点
# （monitor._apply_source_intervals 等）继续按原名字工作。
from mcore.dispatch import (
    SHARD_CURSOR_PREFIX as _SHARD_CURSOR_PREFIX,  # noqa: F401
    apply_proxyless_gate as _apply_proxyless_gate,
    apply_task_sharding as _apply_task_sharding,
    shard_source_tasks as _shard_source_tasks,  # noqa: F401
)
from mcore.sources import (
    AUTO_BOOK_SOURCES as _AUTO_BOOK_SOURCES,  # noqa: F401
    BROWSER_SOURCES as _BROWSER_SOURCES,  # noqa: F401
    PROXYLESS_CAPABLE_SOURCES as _PROXYLESS_CAPABLE_SOURCES,  # noqa: F401
    REST_WAKE_SOURCES as _REST_WAKE_SOURCES,
)
from mcore.scrape_round import (
    _ROUND_FAILURE_PRIORITY,  # noqa: F401
    _SOURCE_DISPATCH_TIMEOUT_SEC,  # noqa: F401
    dispatch_scrape_tasks_async as _dispatch_scrape_tasks_async,  # noqa: F401
    dispatch_with_h2s_circuit as _dispatch_with_h2s_circuit,
    get_browser_executor as _get_browser_executor,  # noqa: F401
    pick_round_failure as _pick_round_failure,  # noqa: F401
    report_source_proxy_failure as _report_source_proxy_failure,  # noqa: F401
    unpack_scrape_result as _unpack_scrape_result,
)
from mcore.stale import (
    full_lifecycle_sources as _full_lifecycle_sources,  # noqa: F401
    mark_stale_listings_for_complete_cities as _mark_stale_listings_for_complete_cities,
    monitored_pairs as _monitored_pairs,
    stale_hours as _stale_hours,
    stale_sweep_decision as _stale_sweep_decision,
    sweep_aging as _sweep_aging,
)
from mcore.startup import (
    apns_startup_diag as _apns_startup_diag,
    bootstrap_settings as _bootstrap_settings,
    check_registry_drift as _check_registry_drift,  # noqa: F401
    reload_settings as _reload_settings,
    setup_logging as _setup_logging,
    validate_structured_config as _validate_structured_config,  # noqa: F401
)
from mcore.telemetry import completeness_stats as _completeness_stats  # noqa: F401
from mcore.outage import (
    OUTAGE_ALERT_BACKOFF as _OUTAGE_ALERT_BACKOFF,  # noqa: F401
    OutageTracker as _OutageTracker,  # noqa: F401
    alert_outage as _alert_outage,
    alert_outage_recovered as _alert_outage_recovered,
    format_duration as _format_duration,
    notify_admin_only as _notify_admin_only,
    outage as _outage,
)
from mcore.notify_round import (
    BATCH_MIN as _BATCH_MIN,  # noqa: F401
    BATCH_SOURCE as _BATCH_SOURCE,  # noqa: F401
    drop_shadow_sources as _drop_shadow_sources,  # noqa: F401
    merge_pending_events as _merge_pending_events,  # noqa: F401
    notify_new_listings as _notify_new_listings,  # noqa: F401
    notify_status_changes as _notify_status_changes,  # noqa: F401
    split_batchable as _split_batchable,  # noqa: F401
)
from mcore.booking_candidates import (
    assign_auto_book_candidates as _assign_auto_book_candidates,  # noqa: F401
    can_auto_book as _can_auto_book,  # noqa: F401
    can_reach_user as _can_reach_user,
    collect_booking_candidates as _collect_booking_candidates,  # noqa: F401
    listing_booking_key as _listing_booking_key,  # noqa: F401
)
from mcore.booking_round import (
    process_booking_results as _process_booking_results,  # noqa: F401
    submit_bookings as _submit_bookings,  # noqa: F401
)

#: 地图坐标补齐的间隔（秒）。半小时一次乘以每批 30 个地址，足以跟上新房源的
#: 出现速度；backlog 大的时候也能在一两天内追平，而不必等管理员想起来点一下。
_GEOCODE_INTERVAL_SEC = 1800
from scrapers import (
    BlockedError,
    OperationNotAllowedError,
    ProxyError,
    RateLimitError,
    ScrapeNetworkError,
    UpstreamMaintenanceError,
    dispatch_scrape_tasks,
)
from update_checker import check_for_updates
from mstorage._notifications import WEB_NOTIFICATIONS_KEEP
from storage import Storage
from users import UserConfig, load_users


# source -> 该 source 专属的长存单线程 executor（见 _get_browser_executor）
_browser_executors: dict[str, ThreadPoolExecutor] = runtime.browser_executors






logger = logging.getLogger("monitor")




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
    peak_intervals = getattr(cfg, "source_peak_min_intervals", None) or {}
    if not intervals and not peak_intervals:
        return tasks

    # 这道闸门**只在高峰期真正起作用**：峰外轮次本身就是 check_interval
    # （300 秒起，实测中位 381 秒），比闸门还慢；峰内轮次是 peak_interval
    # 60 秒、自适应还会衰减到 min_interval 20 秒，闸门成了唯一给 Xior 减速的
    # 东西。七天实测里 98% 的 429 出在高峰那 7 小时。所以峰内单独配一档。
    #
    # 方向和 peak_interval 相反：那个是高峰加快轮次，这个是高峰放慢单个
    # source——正因为轮次加快了才需要它。
    try:
        _, is_peak = get_interval(cfg)
    except Exception:
        # 判不出时段就按峰外处理：宁可少拦一轮，也不能因为读配置出错把
        # source 停掉。
        is_peak = False

    now = time.time() if now is None else now
    out: list = []
    by_source: dict[str, list] = {}
    for t in tasks:
        by_source.setdefault(t.source, []).append(t)

    for src, group in by_source.items():
        # 配置值是**基准**，实际遵守的是乘上自适应倍率之后的值。见
        # mcore/pacing.py：手工调出来的 180 秒实测并不干净，而那套调法默认
        # 「不干净会有人退回上一档」——没有人退。
        #
        # 峰内优先用 source_peak_min_intervals，缺项回落到常规那份。回落而不是
        # 补 0：没为某个 source 配峰内值，意思是「和平时一样」，不是「不限流」。
        if is_peak and src in peak_intervals:
            base_gap = int(peak_intervals.get(src, 0) or 0)
        else:
            base_gap = int(intervals.get(src, 0) or 0)
        gap = _source_pacing.gap_for(src, base_gap)
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
            mult = _source_pacing.multiplier(src)
            logger.info(
                "source %s 距上次抓取仅 %.0f 秒（< %d 秒%s%s），本轮跳过"
                "——抓太频繁会撞限流，反而更慢",
                src, waited, gap,
                "，高峰档" if is_peak and src in peak_intervals else "",
                "" if mult <= 1.0 else f"，基准 {base_gap} ×{mult:.3g}",
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


def _task_labels(tasks) -> list[str]:
    return [f"{t.source}:{t.city_display}" for t in tasks]


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

#: 上游按 operation 白名单拒绝后的冷却（秒）。
#:
#: 这条**不会自己好**——修法只有一个：把站点自己发的那条 operation 原样照抄
#: 回来（docs/H2S.md §5.1）。所以冷却在这里不是「等它恢复」，是**限流**：
#: 不冷却的话每轮（高峰期约 60 秒）都会重跑一次完整抓取 + 浏览器会话，在一个
#: 必然失败的查询上反复烧代理流量。
#:
#: 也不宜太长：修好之后是要发版的，而发版会重启进程、冷却随之清零，所以 15 分钟
#: 只影响「人还没来得及修」的那段窗口。
#:
#: 刻意**不**走 BlockedError 那套指数退避 + 换 IP + 熔断：换多少个 IP 都不会好，
#: 见 scrapers.base.OperationNotAllowedError 的 docstring。
_OPERATION_REJECTED_COOLDOWN = 900  # 15 分钟

# 连续网络失败阈值：连续 N 次全部城市第 1 页网络失败时触发冷却，
# 避免坏代理/断网时监控空转刷屏 error log
_NETWORK_FAIL_THRESHOLD = 3
_NETWORK_FAIL_COOLDOWN = 300  # 5 分钟
_PROXY_CONFIRM_RETRY_DELAY = 60  # 单次代理故障先短冷却复核，不立刻切换/直连
_NATIVE_PROXY_FALLBACK_INTERVAL = 600  # 10 分钟；代理全挂时直连原生 IP 的最高频率

#: 走「自己的线路」（家里那条隧道）时的最小轮次间隔，默认 120 秒。
#:
#: 这是**主动降速**，不是故障降级。商业住宅池的出口烧了换一个就是；自家 IP
#: 烧了没得换，而且影响的是家里所有上网。所以宁可慢，也不能把它打进黑名单。
#:
#: 120 秒是怎么来的：高峰实测轮次中位 26 秒（P90 75 秒），也就是降到约 1/5 的
#: 强度。同时它仍远小于房源的可订窗口——2026-08-25 实测中位 154 分钟、最短
#: 4 分钟，两分钟一轮对最短那种也还有一次机会。再慢就开始真的漏房源了。
#:
#: 可用 PERSONAL_PROXY_MIN_INTERVAL 覆盖：这个值该由线路主人自己定，别人的
#: 家宽和忍耐度都不一样。
def _env_int_positive(name: str, default: int) -> int:
    """读一个正整数环境变量；空/非法/非正一律回落默认值。"""
    return env_int(name, default, positive=True)


_PERSONAL_PROXY_MIN_INTERVAL = _env_int_positive("PERSONAL_PROXY_MIN_INTERVAL", 120)
_PREWARM_CANDIDATE_WAIT_SEC = 2.0  # 有真实候选时最多等 2s 让预登录赶上快速通道

#: 单个 source 一轮抓取的墙钟上限。见 _dispatch_scrape_tasks_async。
#:
#: 取 600 秒的依据：最慢的 source 是 Xior（实测每栋楼 13.9 秒，分片后每轮
#: 上限 8 栋 ≈ 112 秒），留出 5 倍余量。比 CHECK_INTERVAL(300) 大是有意的——
#: 这不是「该多久跑完」的目标，是「卡死了」的判据，宁可宽也不要误杀慢轮。
_H2S_CIRCUIT_BASE_COOLDOWN = 1800  # H2S 抓取 403 后 30 分钟后做 canary
_H2S_CIRCUIT_MAX_COOLDOWN = 21600  # H2S 抓取连续 403 时最多暂停 6 小时
_H2S_LONG_BLOCK_STREAK = 3  # 第 3 次连续 H2S 403 起视为长时间被 block

# 屏蔽通知节流：避免每轮抓取都给用户推一次相同的告警。
# operation 被拒是个**只能靠改代码修好**的状态：不会自愈，也不随时间变化。
# 用 blocked 那 30 分钟的节奏去提醒，等于在人上班之前先发 16 条一模一样的告警。
# 6 小时一条足够——真正的紧迫感来自第一条。
# 维护通知节流：admin 已经在 dashboard banner 上看到维护态了，再叠加 web 通知
# 主要是让 admin 在收 push（如果接了）/ 刷通知面板时也能看到一条记录。
# 间隔比屏蔽长一截——维护态用户什么都做不了，没必要 30 min 一刷。
#
# 这几个节流窗口全部**落库**：以前是模块级 float，注释写着「重启后清零，重启后
# 第一轮会再发通知，符合预期」——那在部署一天 12 次的节奏下就不符合预期了，
# 等于每次部署都给 admin 重发一遍同样的告警。见 mcore/backoff.py。
_throttle_notify_block = runtime.throttles["block"]
_throttle_notify_operation_rejected = runtime.throttles["operation_rejected"]
_throttle_h2s_long_block = runtime.throttles["h2s_long_block"]
_throttle_notify_maintenance = runtime.throttles["maintenance"]


#: 供 _should_notify_* 落库用的 Storage 句柄。由 main() 在启动时设一次。
#:
#: 为什么是模块级而不是参数：这几个 _should_notify_* 散布在十几个调用点，全都
#: 没有 storage 在手，逐个穿参数会把签名污染一大片。和 retry_queue / prewarm_cache
#: 一样按模块级单例处理。为 None 时节流退化成进程内——测试与 CLI 场景正是如此。
def _throttle_storage():
    return runtime.throttle_storage


def _bind_persistent_state(storage) -> None:
    """把落库句柄接上，并从库里恢复所有退避 / 熔断 / 节流状态。

    在 main() 里、进入主循环之前调一次。没有这一步的话这些状态仍然只活在进程内
    ——正是本次要修的那个问题。
    """
    runtime.bind_storage(storage)

    # H2S 详情补齐缓存同样是进程级的。不回填的话，重启后头几轮会把几十条早就
    # 补齐过的房源重新问一遍详情，撞出 429 收手，本轮真正的新房源反而轮不上
    # （见 scrapers/holland2stay.py 的 prime_detail_cache）。
    try:
        from scrapers.holland2stay import prime_detail_cache

        primed = prime_detail_cache(storage.detail_feature_snapshot("holland2stay"))
        if primed:
            logger.info("详情补齐缓存回填 %d 条，重启后不再重复取详情", primed)
    except Exception:
        logger.warning("回填详情补齐缓存失败（按空缓存处理）", exc_info=True)


# ── 全面故障告警 ────────────────────────────────────────────────────
#
# 「某个 source 挂了」「完整率下滑」由 watchdog 负责，但 watchdog 只在**跑完一轮**
# 之后才评估。当所有 source 同时失败时 run_once 直接上抛，那一轮的 watchdog 根本
# 不会执行——最需要告警的场景反而最安静。
#
# 2026-08-05 04:24–09:29 代理断了 5 小时 5 分钟，59 轮全灭，admin 一条告警都没
# 收到：当时 main_loop 的 network / blocked / 内部异常分支只 logger.error。而
# 需要浏览器传输层的 source。它们的 Playwright 对象绑定创建线程，因此**必须**
# 各自跑在 ``_get_browser_executor(source)`` 的专属长存单线程上；放到默认 executor
# 里会因线程漂移抛 ``greenlet.error: Cannot switch to a different thread``。









_PID_FILE = DATA_DIR / "monitor.pid"
_RELOAD_REQUEST_FILE = DATA_DIR / "monitor.reload"

# 热重载事件（SIGHUP → 唤醒 main_loop 中的 sleep，立即重载配置）
_reload_event: asyncio.Event | None = None

# H2S REST 触发器发现新房时设置 → 主循环提前结束等待，跑一轮只抓 H2S 的抓取。
# 见 mcore/h2s_rest.py。
_h2s_wake_event: asyncio.Event | None = None

#: REST 唤醒轮只抓这些 source。

#: 两个唤醒轮之间至少隔多久（从上一轮开始算）。一批房陆续放出时几秒内会有好几次
#: 新 id，一轮 H2S 抓取约 1 秒，没必要每一次都单独跑；也防补抓定时器和新 id
#: 撞在一起时连跑。
_REST_WAKE_MIN_GAP = 5.0


async def _wait_reload_or_wake(timeout: float) -> str:
    """等热重载或 REST 唤醒，返回 ``"reload"`` / ``"wake"`` / ``"timeout"``。

    两个都已设置时热重载优先：配置变了，先换上新配置再跑哪一轮都对。
    """
    events: dict[str, asyncio.Event] = {}
    if _reload_event is not None:
        events["reload"] = _reload_event
    if _h2s_wake_event is not None:
        events["wake"] = _h2s_wake_event
    for name in ("reload", "wake"):
        if name in events and events[name].is_set():
            # 已经置位也先让出一次：调用方要是忘了 clear，下一圈又立刻返回，
            # 不让出就是一个饿死整个事件循环的忙等（REST 协程、超时全都不动）。
            await asyncio.sleep(0)
            return name
    if not events:
        await asyncio.sleep(timeout)
        return "timeout"
    waiters = {asyncio.ensure_future(ev.wait()): name for name, ev in events.items()}
    try:
        done, _ = await asyncio.wait(waiters, timeout=timeout,
                                     return_when=asyncio.FIRST_COMPLETED)
    finally:
        for w in waiters:
            if not w.done():
                w.cancel()
    fired = {waiters[w] for w in done}
    for name in ("reload", "wake"):
        if name in fired:
            return name
    return "timeout"

# 类型别名：每个用户与其对应通知器的配对列表
UserNotifiers = list[tuple[UserConfig, BaseNotifier]]

# mcore 服务实例（进程生命周期内单例）
prewarm_cache = runtime.prewarm_cache
#: H2S 熔断 / 登录抑制的退避状态。**落库，跨进程重启存活**——见 mcore/backoff.py。
#:
#: 这些以前是裸的模块级 float（基于 time.monotonic），重启即清零。也就是说正在被
#: CF 封、退避已拉到 6 小时的时候部署一次，立刻满速重打。2026-08-20 一天部署 12 次
#: = 12 次退避清零。
#: 每个 source 一个熔断器，策略按 source 配（mcore/circuit.py）。以前只有 H2S 有
#: ——而实测整源失败次数是 xior 147 / ourdomain 67 / ourcampus 60 / H2S 6，
#: 保护恰好装在最不需要它的那个上。
_source_circuits = runtime.source_circuits

#: 按 429 历史自动伸缩各 source 的最小抓取间隔。和熔断分工不同：熔断管「出事
#: 之后停多久」，这个管「平时多久打一次」。只有熔断的话，冷却一结束节奏就回到
#: 原值，过三五十分钟再撞一次——生产实测每天约 10 次，从没停过。
_source_pacing = runtime.source_pacing
_h2s_login_block = runtime.h2s_login_block


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
    run_once 只在「所有 source 都失败」时上抛，那种情况由 main_loop 的
    ``_OutageTracker`` 直接告警（见 ``_alert_outage``），这里再报一遍只是重复。
    遥测行在上抛之前就已经写好，所以恢复后的第一轮巡检仍能看到这段历史。

    这段话原先写的是「main_loop 的 network / blocked 连续失败计数已经在告警了」
    ——**当时那几个分支只 logger.error，一条通知都不发**。于是 2026-08-05 代理断
    线 5 小时、59 轮全灭，admin 全程静默：部分退化会告警，全面宕机反而不会。
    ``_OutageTracker`` 就是来补上这个前提的；改动这里之前请确认它仍然成立
    （tests/test_outage_alert.py 守着这条）。

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












async def run_once(
        cfg,
        storage: Storage,
        user_notifiers: UserNotifiers,
        *,
        web_notifier: WebNotifier | None = None,
        dry_run: bool = False,
        booking_deadline: float = float("inf"),
        only_sources: frozenset[str] | None = None,
) -> dict[str, bool]:
    """
    执行一次完整的「抓取 → 对比 → 通知 → 自动预订」流程。

    ``only_sources`` 给了就只抓这些 source（H2S REST 触发器叫醒的那一轮只抓
    H2S，见 mcore/h2s_rest.py）。其余流程不变。

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

    # 整轮是否跨多个 source——按**配置**算，在任何筛选之前。completeness key
    # 的 source 前缀靠它（scrapers.completeness_key）。按筛剩的任务算会出错：
    # 节流跳过 Xior、或者 REST 唤醒轮只抓 H2S 的时候，剩下只有 H2S，前缀消失，
    # 「Eindhoven」这个裸 key 让 mark_stale_listings 退化成不带 source 的
    # ``city IN (...)``，用 H2S 的完整性把同城 Xior 这轮没抓的房源一起老化掉。
    multi_source = len(source_totals) > 1

    if only_sources is not None:
        scrape_tasks = [t for t in scrape_tasks if t.source in only_sources]
        if not scrape_tasks:
            logger.info("本轮只抓 %s，但它们都不在配置里，跳过", sorted(only_sources))
            return {}

    # 代理全挂时只留下**直连打得通**的 source。放在节流和分片之前：被代理
    # 挡住的 source 本轮根本不该占分片游标。
    scrape_tasks = _apply_proxyless_gate(scrape_tasks)

    # 先按 source 节流（多久抓一次），再分片（每轮抓几个）——两者管的不是
    # 同一件事，顺序不能反：先分片会让被跳过的 source 白白推进分片游标。
    scrape_tasks = _apply_source_intervals(scrape_tasks, cfg, storage, dry_run=dry_run)
    scrape_tasks = _apply_task_sharding(scrape_tasks, cfg, storage, dry_run=dry_run)

    loop = asyncio.get_running_loop()

    prewarm = PrewarmRound(loop, storage, user_notifiers, dry_run)

    try:
        fresh, completeness, h2s_blocked, h2s_mode = await _dispatch_with_h2s_circuit(
            loop, scrape_tasks, storage=storage, source_totals=source_totals,
            multi_source=multi_source, dry_run=dry_run,
        )
        _log_scrape_completeness(completeness)
        stop = await _handle_h2s_blocked(
            h2s_blocked, h2s_mode, fresh, completeness,
            storage=storage, web_notifier=web_notifier, dry_run=dry_run,
        )
        if stop is not None:
            return stop
    except Exception as e:
        # 失败分支自己 stash prewarm；多数会重抛给 main_loop 做退避。
        return await _handle_scrape_failure(
            e,
            storage=storage, web_notifier=web_notifier,
            scrape_tasks=scrape_tasks, user_notifiers=user_notifiers,
            dry_run=dry_run, prewarm=prewarm,
        )

    logger.info("本次抓取共 %d 条房源", len(fresh))

    if dry_run:
        _print_dry_run(fresh, user_notifiers)
        return completeness

    return await _run_post_scrape(
        cfg, storage, user_notifiers, fresh, completeness,
        web_notifier=web_notifier, dry_run=dry_run,
        booking_deadline=booking_deadline, loop=loop, prewarm=prewarm,
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
    global _reload_event, _h2s_wake_event
    _reload_event = asyncio.Event()
    _h2s_wake_event = asyncio.Event()
    # REST 触发器常驻后台。开关 H2S_REST_ENABLED 每个 tick 现读，关着的时候它
    # 一个请求都不发，所以无条件启动。lambda 读的是本函数的局部 cfg，热重载
    # 换掉 cfg 之后它自然看到新的。
    rest_trigger = RestTrigger(_h2s_wake_event)
    # 保留引用：asyncio 文档要求调用方持有 task 的强引用，否则可能被 GC。
    _rest_task = asyncio.create_task(rest_trigger.run(lambda: cfg))

    round_count = 0
    last_heartbeat_time = time.monotonic()  # 启动时记为刚发过，避免第一轮立即心跳
    last_stale_sweep_time = 0.0  # 启动后第一轮成功抓取即执行一次状态收敛
    # 地图坐标补齐用自己的计时器，不搭心跳的车：心跳是**通知**，把
    # HEARTBEAT_INTERVAL_MINUTES 设成 0 是「别给我发心跳」，不该顺带让地图
    # 停止补坐标。0.0 表示启动后第一轮就补一次。
    last_geocode_time = 0.0
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
            # 摘要打「他实际会碰到哪些平台、各自凭据齐没齐」。此前只打 ab.email
            # （H2S 的），于是一个只订 Plaza 的用户永远显示「账号: (未设置)」。
            targets = _autobook_targets(user)
            gaps = _autobook_credential_gaps(user)
            logger.info(
                "自动预订 [%s]: %s  平台: %s  凭据: %s",
                user.name, mode, "/".join(targets) or "(无)",
                "缺 " + "；".join(gaps) if gaps else "齐",
            )
            # 自动预订开启时必须能通知到用户，否则付款链接 / 结果无法送达。
            # 判据与 _can_reach_user 同一条——只看 notification_channels 会把
            # 「只用 App、靠推送」的用户误报成配置不全（2026-09-10 线上真实案例）。
            if not user.notifications_enabled:
                logger.warning(
                    "⚠️  [%s] 自动预订已开启，但该用户通知已关闭（notifications_enabled=false）！"
                    "预订成功后付款链接将无法送达，请开启通知或在日志中查找 CRITICAL 行。",
                    user.name,
                )
            elif not _can_reach_user(user, notifier, storage):
                logger.warning(
                    "⚠️  [%s] 自动预订已开启，但既没有通知渠道也没有活跃设备！"
                    "预订结果将无法送达——该用户本轮不会产生任何自动预订候选。"
                    "请添加 iMessage/Telegram/Email/WhatsApp 渠道，或在手机 App 上登录一次。",
                    user.name,
                )

            for gap in gaps:
                logger.warning(
                    "⚠️  [%s] 自动预订已开启，但缺少 %s——该平台不会产生候选。",
                    user.name, gap,
                )
        else:
            logger.info("自动预订 [%s]: 已关闭", user.name)

    network_fail_streak = 0  # ScrapeNetworkError 连续计数
    # 下一轮是不是 REST 唤醒的（只抓 H2S）；以及被唤醒打断时原计划的等待截止
    # 时刻——唤醒轮跑完回到原计划，不重新起算一整个间隔，否则一批房陆续放出的
    # 那几分钟里，其它 source 会被一直往后推。
    next_round_rest_wake = False
    planned_deadline: float | None = None
    last_round_started = float("-inf")
    blocked_fail_streak = 0  # Cloudflare 403 连续计数

    while True:
        round_count += 1
        rest_wake_round = next_round_rest_wake
        next_round_rest_wake = False
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

            # 主动降速：走自家线路时把轮次间隔抬到 _PERSONAL_PROXY_MIN_INTERVAL。
            # 放在 native fallback **之前**判定，两者互斥（个人代理还活着就不算
            # 全冷却），但真同时成立时下面的 max 会取更慢的那个，方向正确。
            personal_proxy_active = is_personal_proxy_active()
            if personal_proxy_active:
                prev_personal = effective_interval
                effective_interval = max(effective_interval, _PERSONAL_PROXY_MIN_INTERVAL)
                if effective_interval != prev_personal:
                    logger.info(
                        "🏠 正在使用自己的线路，主动降速：本轮间隔 %d 秒（原 %d 秒）"
                        "——自家 IP 烧了没得换",
                        effective_interval, prev_personal,
                    )

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

            if rest_wake_round:
                peak_tag = f"{peak_tag}【REST 唤醒·只抓 H2S】"
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
            # 任何一轮只要抓了 H2S，就顺带核对 REST 报过的 id——所以每轮都清掉
            # 唤醒标记，不只是唤醒轮：正常轮刚抓完，没必要再叫醒一次。
            _h2s_wake_event.clear()
            rest_check_ids = rest_trigger.begin_round()
            round_started_at = datetime.now(timezone.utc)
            last_round_started = time.monotonic()
            city_completeness = await run_once(
                cfg,
                storage,
                user_notifiers,
                web_notifier=web_notifier,
                booking_deadline=booking_deadline,
                only_sources=_REST_WAKE_SOURCES if rest_wake_round else None,
            )
            try:
                rest_trigger.end_round(rest_check_ids, storage, round_started_at)
            except Exception:
                logger.exception("核对 REST 发现的房源失败（已忽略）")

            # 全面故障结束。紧跟 run_once 之后判定，不放到本段末尾——下面那些
            # 收尾步骤（剪枝、watchdog）自己也可能抛，那会被 except Exception 记成
            # 新一轮「全面故障」，而抓取明明成功了。告警过才回报恢复（见
            # record_success）。
            recovered = _outage.record_success(time.monotonic())
            if recovered:
                span, failed_rounds = recovered
                logger.info(
                    "✅ 全面故障已恢复：持续 %s，期间 %d 轮全失败",
                    _format_duration(span), failed_rounds,
                )
                await _alert_outage_recovered(storage, web_notifier, span, failed_rounds)

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

            # 成功：高峰期将自适应间隔缩短 5%（逐步逼近 min_interval）。
            # 唤醒轮不算：它不是按间隔排出来的，算进去会让一批房放出的那几秒把
            # 自适应间隔连压好几档。
            if is_peak and not rest_wake_round:
                prev = adaptive_peak
                adaptive_peak = max(float(cfg.min_interval), adaptive_peak * _ADAPTIVE_DECREASE)
                if int(prev) != int(adaptive_peak):
                    logger.info(
                        "🔽 自适应间隔: %d → %d 秒（下限 %d 秒）",
                        int(prev), int(adaptive_peak), cfg.min_interval,
                    )

            # 状态收敛：仅对本轮完整扫描成功的城市执行。
            # 整轮连接失败会在 run_once() 抛出并跳过这里；部分城市不完整则不收敛该城市。
            #
            # 老化那两段**每轮都跑**：它们是小时级的，等 24 小时那一趟等于阈值
            # 白调。每轮跑没有累积开销——到终态的行之后一律被 WHERE 排除。
            try:
                aged = _sweep_aging(storage, city_completeness)
                if aged:
                    r_h, o_h = _stale_hours()
                    logger.info(
                        "已收敛 %d 条未见 listing（消失 %gh 转推测 Reserved，"
                        "%gh 判 Occupied——后者对齐 H2S 官方付款限时）",
                        aged, r_h, o_h,
                    )
            except Exception:
                logger.exception("老化收敛失败（已忽略）")

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
                        monitored_pairs=_monitored_pairs(cfg),
                        orphan_days=30,
                    )
                    if stale:
                        logger.info(
                            "孤儿收敛：已将 %d 条未见 listing 推测为 Occupied"
                            "（已移出监控范围的城市，宽限期 30 天）",
                            stale,
                        )
                except Exception:
                    logger.exception("mark_stale_listings 失败（已忽略）")
                finally:
                    last_stale_sweep_time = time.monotonic()

            heartbeat_interval_sec = cfg.heartbeat_interval_minutes * 60
            if heartbeat_interval_sec > 0 and time.monotonic() - last_heartbeat_time >= heartbeat_interval_sec:
                total = storage.count_all()
                # 心跳只发给 admin。它回答的是「监控还在跑吗」——那是运维问题，
                # 普通用户既无从判断也无从处置，而每小时一条推送足够让人把整个
                # 通知渠道静音，连真正的房源通知一起埋掉。
                if web_notifier:
                    await web_notifier.send_heartbeat(total_in_db=total, round_count=round_count)
                # 清理旧通知，防止 web_notifications 表无限增长
                pruned = storage.prune_notifications(keep=WEB_NOTIFICATIONS_KEEP)
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

            # 给新房源补地图坐标。此前只有管理员在地图页手动点才会解析，于是新
            # 抓到的房源在有人想起来点一下之前都不在图上。
            #
            # 每批有上限（见 mcore.geocode.DEFAULT_BATCH）：稳态下每次只有零星
            # 几个新地址，但第一次跑或换了监控城市之后会有几百个，不设上限就会
            # 把一个抓取轮次拖成几分钟。剩下的交给下一次。
            if time.monotonic() - last_geocode_time >= _GEOCODE_INTERVAL_SEC:
                try:
                    ok, bad = _geocode.geocode_missing(storage)
                    if ok or bad:
                        logger.info("地图坐标补齐：成功 %d，失败 %d", ok, bad)
                except Exception:
                    logger.exception("geocode_missing 失败（已忽略）")
                finally:
                    last_geocode_time = time.monotonic()

            loop = asyncio.get_running_loop()
            if (rest_wake_round and planned_deadline is not None
                    and planned_deadline > loop.time()):
                # 唤醒轮插在原计划的等待中间：跑完回到原计划，不重新起算。
                deadline = planned_deadline
                logger.info("REST 唤醒轮结束，回到原计划：再等 %d 秒",
                            int(deadline - loop.time()))
            else:
                actual = apply_jitter(effective_interval, cfg.jitter_ratio)
                # 抖动是 ±jitter_ratio，会把间隔往下拉——下限必须在抖动之后再夹一次，
                # 否则 0.4 的抖动能把 120 秒打到 72 秒，降速就漏了。
                if personal_proxy_active:
                    actual = max(actual, _PERSONAL_PROXY_MIN_INTERVAL)
                if native_fallback_active:
                    actual = max(actual, _NATIVE_PROXY_FALLBACK_INTERVAL)
                dev_pct = (actual - effective_interval) / effective_interval * 100
                logger.info(
                    "等待 %d 秒（基准 %d s，%+.0f%%）%s",
                    actual, effective_interval, dev_pct,
                    "（高峰期自适应）" if is_peak else "",
                )
                deadline = loop.time() + float(actual)
            planned_deadline = deadline

            # 等待下一轮：超时正常继续；SIGHUP 或 reload 文件触发则热重载；
            # REST 发现新房则提前结束、下一轮只抓 H2S。
            # Windows 不支持可靠的 SIGHUP，因此每秒轮询一次 reload 请求文件。
            reload_triggered = False

            while True:
                if _consume_reload_request_file():
                    logger.info("检测到文件触发的热重载请求")
                    reload_triggered = True
                    break

                remaining = deadline - loop.time()
                if remaining <= 0:
                    break

                fired = await _wait_reload_or_wake(min(1.0, remaining))
                if fired == "reload":
                    reload_triggered = True
                    break
                if fired == "wake":
                    _h2s_wake_event.clear()
                    if not rest_trigger.has_due():
                        # 补抓定时器到点时那个 id 已经被别的轮次核对掉了
                        continue
                    gap = _REST_WAKE_MIN_GAP - (time.monotonic() - last_round_started)
                    if gap > 0:
                        await asyncio.sleep(min(gap, max(0.0, deadline - loop.time())))
                    next_round_rest_wake = True
                    break

            if reload_triggered:
                _reload_event.clear()
                logger.info("热重载中...")
                load_dotenv(dotenv_path=ENV_PATH, override=True)
                # override=True 把 .env 重放了一遍，runtime 配置得跟着刷新，
                # 否则面板刚改的值这一程都不生效。顺序不能反：先 .env 后数据库。
                _reload_settings()
                try:
                    cfg = load_config()
                    users = load_users()
                    # **先构造新的，成功之后再关旧的。** 反过来的话，构造一抛错，
                    # 下面那个 except 会打印「继续使用旧配置」——而旧的 notifier
                    # 已经 close 掉了，于是**所有渠道永久哑掉**直到进程重启，
                    # 日志上还写着一切照旧。
                    fresh_notifiers = _build_user_notifiers(users)
                    for _, n in user_notifiers:
                        try:
                            await n.close()
                        except Exception:
                            # 关旧的失败最多泄漏一个 session，不该连累这次热重载
                            logger.warning("关闭旧 notifier 失败（忽略）", exc_info=True)
                    user_notifiers = fresh_notifiers
                    # 用户可能改了密码/邮箱/账号 → 全量失效 prewarm 缓存。
                    # 下一轮 run_once 会按需重建（命中策略已对齐 active_user_ids）。
                    prewarm_cache.clear()
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
            if _outage.record_failure(time.monotonic()):
                await _alert_outage(
                    storage, web_notifier,
                    "所有 source 被 Cloudflare 屏蔽（403）",
                    "退避已拉长到最多 2 小时。持续屏蔽请换 HTTPS_PROXY 出口，"
                    "或暂停几小时让 WAF 状态冷下来。",
                )
            await asyncio.sleep(cooldown)
        except OperationNotAllowedError:
            # 上游按 operation 白名单拒绝。**不会自己好**，冷却在这里是限流不是
            # 等待：不冷却的话每轮都在一个必然失败的查询上重跑完整抓取 + 浏览器
            # 会话。修好要发版，发版会重启进程、冷却清零，所以这 15 分钟只影响
            # 「人还没来得及修」的那段窗口。
            #
            # 刻意不做指数退避、不换 IP、不进熔断——换多少个 IP 都不会好。
            cooldown = apply_jitter(_OPERATION_REJECTED_COOLDOWN, cfg.jitter_ratio)
            logger.error(
                "⛔ 上游按 operation 白名单拒绝，冷却 %d 秒。"
                "这条不会自己恢复，需要照抄站点那条 operation（docs/H2S.md §5.1）。",
                cooldown,
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
                if _outage.record_failure(time.monotonic()):
                    await _alert_outage(
                        storage, web_notifier, "代理失效且无备用可切", str(e),
                    )
                await asyncio.sleep(cooldown)
        except ScrapeNetworkError as e:
            network_fail_streak += 1
            if network_fail_streak >= _NETWORK_FAIL_THRESHOLD:
                cooldown = apply_jitter(_NETWORK_FAIL_COOLDOWN, cfg.jitter_ratio)
                # 不要在这里替用户断言「是代理/网络的问题」。走到这条分支只说明
                # **所有 source 都失败了**，成因可能是代理、网络，也可能是某个
                # 平台改了 API——2026-08-11 与 08-17 两次 H2S 端点迁移，这句
                # 「请检查代理/网络」各刷了三天与半天，而代理自始至终正常，
                # 排查方向被它带偏了两次。异常文本里已经带着真正的成因，让它说话。
                logger.error(
                    "🌐 连续 %d 次网络失败（阈值 %d），冷却 %d 秒。"
                    "本轮全部 source 均未取到数据 — 成因见下方异常文本，"
                    "代理故障会另有「代理失效」告警。最近错误: %s",
                    network_fail_streak, _NETWORK_FAIL_THRESHOLD, cooldown, e,
                )
                # 达阈值才告警：低于阈值的抖动通常几轮内自愈。异常文本里带着
                # _describe_navigation_failure() 的代理探测结论（配额耗尽 / 认证
                # 失败 / 代理宕机），直接透出去，省掉一轮上服务器翻日志。
                if _outage.record_failure(time.monotonic()):
                    await _alert_outage(
                        storage, web_notifier,
                        "所有 source 网络不可达",
                        f"请检查代理与网络。最近错误：\n{e}",
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
            # 反复抛的内部异常（DB 锁死、磁盘满）同样让每一轮都跑不完，watchdog
            # 一样评估不到。告警通道本身出问题时不能再把主循环带崩。
            if _outage.record_failure(time.monotonic()):
                try:
                    await _alert_outage(
                        storage, web_notifier,
                        "主循环反复抛出未预期异常",
                        f"{type(e).__name__}: {e}",
                    )
                except Exception:
                    logger.debug("全面故障告警发送失败（已忽略）", exc_info=True)
            await asyncio.sleep(10)


# ------------------------------------------------------------------ #
# 入口
# ------------------------------------------------------------------ #

def _autobook_targets(user) -> list[str]:
    """这个用户的自动预订实际会碰到哪些平台。

    ``allowed_sources`` 为空 = 不限平台，那就是所有支持下单的 source。非空时取交集
    ——用户只订 Plaza 的话，H2S 凭据缺不缺跟他没关系。

    与 ``_AUTO_BOOK_SOURCES`` 取交集而不是直接用 ``allowed_sources``：后者可以写进
    还没实现 booker 的平台，据此去警告「没填凭据」会让人去填一个填了也没用的东西。
    """
    allowed = {s.strip().lower()
               for s in (user.auto_book.listing_filter.allowed_sources or [])
               if s and s.strip()}
    targets = set(_AUTO_BOOK_SOURCES)
    return sorted(targets & allowed) if allowed else sorted(targets)


def _autobook_credential_gaps(user) -> list[str]:
    """他会碰到的平台里，哪些缺凭据。返回人话，供启动摘要告警。

    **按平台分别判**。此前这里无条件检查 H2S 的 email / password，于是一个
    ``allowed_sources=['plaza']`` 的用户每次启动都吃两条「未填写 H2S 账号」——
    他根本不抓 H2S。运维日志里的警告要是经常是假的，真的那条就没人看了。
    """
    ab = user.auto_book
    gaps: list[str] = []
    for src in _autobook_targets(user):
        if src == H2S_SOURCE and not (ab.email and ab.password):
            gaps.append("H2S 账号邮箱/密码（AUTO_BOOK_EMAIL / AUTO_BOOK_PASSWORD）")
        elif src == "plaza":
            if not (ab.plaza_username and ab.plaza_password):
                gaps.append("Plaza 用户名/密码")
            elif not ab.plaza_enabled:
                gaps.append("Plaza 开关（凭据已填但「开启 Plaza 自动申请」没勾）")
    return gaps


async def _async_main() -> None:
    parser = argparse.ArgumentParser(description="Holland2Stay 房源监控")
    parser.add_argument("--once", action="store_true", help="只运行一次后退出")
    parser.add_argument("--test", action="store_true", help="抓取并打印，不写库不发通知")
    parser.add_argument("--reset-db", action="store_true", help="启动前清空数据库（非交互式）")
    args = parser.parse_args()

    # 强制从 .env 文件重新加载（override=True 覆盖继承的环境变量），
    # 确保子进程启动时使用最新的 .env 配置而非父进程的陈旧值。
    load_dotenv(dotenv_path=ENV_PATH, override=True)
    # 日志必须先配。迁移与键名审计都在 _bootstrap_settings() 里，它们打的
    # logger.info 若无 handler 会被直接丢弃——2026-08-06 上线时实测：迁移正常
    # 完成，日志里却一个字都没有，而文档恰恰让人去日志里核对搬了哪些键。
    #
    # LOG_LEVEL 本身也是 runtime 键，此刻还没注水，因此先用环境里的值起个头，
    # 注水之后再按最终配置重配一次。_setup_logging 每次都会重建 handler。
    _setup_logging(os.environ.get("LOG_LEVEL", "INFO"))

    # runtime 类配置住在 app_settings 表里，必须在 load_config() **之前**注入
    # os.environ——load_config() 读的就是 os.environ，晚一步就读到默认值了。
    #
    # 迁移只在 monitor 这一处做：web 有多个 gunicorn worker，并发改写 .env 会
    # 打架。两个进程读同一个库，monitor 搬完 web 下次注水就看得到。
    _bootstrap_settings()

    cfg = load_config()
    _setup_logging(cfg.log_level)

    # 键名审计放在日志配好之后、抓取开始之前。打错的键此前是完全静默的：
    # PEAK_STRAT=08:30 不报错，只是安静地走默认值，而你以为自己改了。
    # 只 WARNING 不阻断——一个拼错的键让整个监控起不来，代价远大于它本身。
    #
    # 只挂在 monitor 这一处：web 走 gunicorn 时不经过 main()，挂到模块导入上则
    # 每个 worker 各刷一遍。两个进程读的是同一个 .env，报一次就够。
    from env_registry import log_env_audit
    log_env_audit(ENV_PATH)

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
    _bind_persistent_state(storage)

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
        # 常驻浏览器不归 prewarm_cache 管（借出去的 session 一律不许关它），
        # 所以进程退出要在这里单独关一次，否则漏一个 Chromium。
        try:
            from mcore.warm_browser import warm_lane
            warm_lane.close(reason="monitor 退出")
        except Exception:
            logger.debug("关闭常驻浏览器失败（已忽略）", exc_info=True)
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
