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

from booker import PrewarmedSession
from config import (DATA_DIR, ENV_PATH, get_proxy_url, is_personal_proxy_active,
                    is_proxy_native_fallback_active, load_config)
from models import STATUS_AVAILABLE
from notifier import BaseNotifier, WebNotifier, create_user_notifier
from mcore.backoff import PersistedBackoff
from mcore.circuit import SourceCircuits
from mcore.health import CIRCUIT_OPEN_ERROR
from mcore.pacing import AdaptivePacing
from mcore.booking import RetryQueue, area_key, book_with_fallback
from mcore.interval import apply_jitter, get_interval
from mcore import geocode as _geocode
from mcore.prewarm import PrewarmCache
from scrapers import (
    BlockedError,
    OperationNotAllowedError,
    ProxyError,
    RateLimitError,
    ScrapeNetworkError,
    UpstreamMaintenanceError,
    completeness_key,
    dispatch_scrape_tasks,
    is_proxy_account_error,
    is_proxy_error,
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
    dispatcher 自己看不出整轮是不是多源（见 ``scrapers.completeness_key``）。
    """
    executor = (
        _get_browser_executor(browser_source or _H2S_SOURCE) if isolated else None
    )
    fut = loop.run_in_executor(
        executor,
        lambda: dispatch_scrape_tasks(selected, multi_source=multi_source),
    )
    # 墙钟兜底。**没有它的话，渲染器一卡就是整个监控停摆**：
    # ``page.evaluate`` 在 Playwright 里根本没有 timeout 参数（它等的是 JS
    # promise，``set_default_timeout`` 也管不到），``page.content()`` 同样。
    # 一个 wedged 页面会让浏览器线程永远停在那里，而这里的 await 没有上限，
    # run_once 就不返回了——表现是 last_round_at 不断变老，与「进程挂了」无从区分。
    #
    # ⚠️ 超时**救不回那条线程**：run_in_executor 的 future 取消不了底层调用，
    # 卡住的浏览器线程会一直占着。这里能做到的是让**本轮继续走完**（其余
    # source 照常入库、通知照发），并把这个 source 报成失败，由既有的隔离 /
    # 熔断路径处置。真正的修复是让浏览器侧自己有超时；在那之前，这一层保证
    # 一个源的死锁不会变成全局死锁。
    try:
        return await asyncio.wait_for(fut, timeout=_SOURCE_DISPATCH_TIMEOUT_SEC)
    except asyncio.TimeoutError as e:
        src = browser_source or (selected[0].source if selected else "?")
        logger.error(
            "source %s 抓取超过 %.0f 秒没返回，判为卡死并放弃本轮"
            "（%d 个任务；该线程可能仍占着，下轮会重建）",
            src, _SOURCE_DISPATCH_TIMEOUT_SEC, len(selected),
        )
        raise ScrapeNetworkError(
            f"{src} 抓取超时（>{_SOURCE_DISPATCH_TIMEOUT_SEC:.0f}s），疑似渲染器卡死"
        ) from e


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

    **可重入。** 启动时会调两次：第一次用环境里的 LOG_LEVEL 起个头，好让
    _bootstrap_settings() 里的迁移日志有地方去；注水之后再按最终配置调一次。
    每次先摘掉自己上一轮装的 handler，否则每行日志会被写两遍。
    """
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    root = logging.getLogger()
    for h in [h for h in root.handlers if getattr(h, "_monitor_owned", False)]:
        root.removeHandler(h)
        h.close()
    logging.basicConfig(level=getattr(logging, level, "INFO"), format=fmt)
    root.setLevel(getattr(logging, level, logging.INFO))
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
    main_fh._monitor_owned = True
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
    error_fh._monitor_owned = True
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


#: 代理全挂、降级直连原生 IP 时，哪些 source 还能抓。
#:
#: 2026-08-26 在生产服务器上逐个实测（webshare 两个账号同时 402，正好是干净的
#: 直连环境）：
#:
#:     OurDomain    HTTP 200,  8.9 KB      ✅
#:     OurCampus    HTTP 200, 62.8 KB      ✅
#:     H2S          真浏览器解 CF 挑战，90 秒未解开 ×3   ❌
#:     Xior         同上，停在挑战页                    ❌
#:
#: 判据是**实测**不是猜的：两家 RENTCafe 站点在机房 IP 上根本没有 Cloudflare
#: 挑战，而 H2S / Xior 是硬挡——不是慢，是挑战解不开。
#:
#: 名单写死而不是自动探测：自动探测意味着每轮都要先打一次试探请求，代价是
#: 额外流量和额外的被封面。上游哪天变了，这里会表现为「降级期间某个 source
#: 一直失败」，日志里看得见。
_PROXYLESS_CAPABLE_SOURCES: frozenset[str] = frozenset({"ourdomain", "ourcampus"})


def _apply_proxyless_gate(tasks: list) -> list:
    """代理全挂时，把直连打不通的 source 整体摘出本轮。

    为什么要摘
    ----------
    2026-08-26 生产实况：webshare 两个账号同时 402，五次「降级直连原生 IP」，
    **一轮都没抓成**——H2S / Xior 在直连下必然失败，而
    ``dispatch_scrape_tasks`` 的判据是「全员失败就上抛 ProxyError」，于是那两家
    的必然失败把同一轮里本来能成的 OurDomain / OurCampus 一起掀掉了。

    摘掉之后这一轮就只剩打得通的那些，成功数不再是 0，整轮也就不会被上抛的
    ProxyError 中止。代价是 H2S / Xior 在代理恢复前完全不抓——但它们本来也
    抓不到，区别只是「安静地等」还是「每轮制造一批注定失败的请求」。

    这也让遥测说了实话：被摘掉的 source 本轮不写 round_stats，健康判定看到的
    是「没有这一轮」，而不是「这一轮失败了」。把等代理算成抓取失败，会让
    fail_streak 报出一个和成因无关的结论——判据和被判的东西不是一回事。
    """
    from config import is_proxy_native_fallback_active

    try:
        if not is_proxy_native_fallback_active():
            return tasks
    except Exception:
        # 判不出代理状态就当作正常：宁可让它去试一轮，也不能因为读状态出错
        # 把 source 静默停掉。
        logger.debug("代理降级状态判定失败，本轮不启用直连闸门", exc_info=True)
        return tasks

    kept, dropped = [], []
    for t in tasks:
        (kept if (t.source or "") in _PROXYLESS_CAPABLE_SOURCES else dropped).append(t)

    if dropped:
        by_source: dict[str, int] = {}
        for t in dropped:
            by_source[t.source] = by_source.get(t.source, 0) + 1
        logger.warning(
            "🛰️ 代理全部失效，本轮跳过直连打不通的 source：%s"
            "（它们在机房 IP 上会被 Cloudflare 挡住，不是抓取故障）｜"
            "仍在抓：%s",
            "、".join(f"{k}×{v}" for k, v in sorted(by_source.items())),
            "、".join(sorted({t.source for t in kept})) or "无",
        )
    return kept


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


#: 「消失多久算不再可订 / 算彻底没了」。四个平台、所有状态统一一套。
#:
#: 曾经按 (source, 状态类) 分开配过，最后收掉了：那些差别描述的是「feed 会不会
#: 保留下架房源」，而实测下来四个平台的终态都是**从 feed 里消失**——只有 Xior
#: 的 feed 里真有 Occupied，其余三个平台的终态基本全靠推。既然消失是共同的
#: 下架信号，就不该有四套判据。
#:
#: 数怎么定的：轮次约 1 分钟一次，30 分钟 ≈ 30 轮连续完整扫描里都没有它，
#: 而同一次响应里通常还有二十几条别的房源作旁证。OurDomain / OurCampus 另有
#: 一道闸——``scrapers.ourdomain`` 要求连续 3 轮返回 0 个单元才承认「真没房」。
#:
#: 收得太早的代价不是「少通知」而是「多通知」：房源被误判之后又出现在 feed 里，
#: 会产生一次状态变更，用户收到一条假的重新上架。中间那站 Reserved 就是为此
#: 存在的——2 小时内回来只是 ``Reserved → 可订``，是最常见的正常迁移。
#: 地图坐标补齐的间隔（秒）。半小时一次乘以每批 30 个地址，足以跟上新房源的
#: 出现速度；backlog 大的时候也能在一两天内追平，而不必等管理员想起来点一下。
_GEOCODE_INTERVAL_SEC = 1800

_STALE_RESERVED_HOURS = 0.5
#: 2 小时对齐 **H2S 官方的付款限时**：消失超过它，预留必然已经落定。
_STALE_OCCUPIED_HOURS = 2.0


def _stale_hours() -> tuple[float, float]:
    """``(消失多久转 Reserved, 消失多久判 Occupied)``，环境变量可覆盖。"""
    def _read(name: str, default: float) -> float:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        try:
            return max(0.25, min(24.0 * 30, float(raw)))
        except ValueError:
            logger.warning("%s=%r 不是数字，改用默认值 %s", name, raw, default)
            return default

    reserved = _read("STALE_RESERVED_HOURS", _STALE_RESERVED_HOURS)
    occupied = _read("STALE_OCCUPIED_HOURS", _STALE_OCCUPIED_HOURS)
    # 终态窗口必须 >= 中间站，否则第二段会抢在第一段之前把房源直接判死，
    # 中间那一站形同虚设——而它正是「判错时代价小」的全部来源。
    if occupied < reserved:
        logger.warning(
            "STALE_OCCUPIED_HOURS(%s) 小于 STALE_RESERVED_HOURS(%s)，"
            "会让 Reserved 那一站失效；已按 reserved 取齐。", occupied, reserved,
        )
        occupied = reserved
    return reserved, occupied


def _mark_stale_listings_for_complete_cities(
        storage: Storage,
        completeness: dict[str, bool],
        *,
        monitored_pairs: list[tuple[str, str]] | None = None,
        orphan_days: int = 30,
) -> int:
    """只对本轮完整扫描成功的城市执行 stale listing 收敛。

    key 带 ``source:`` 前缀时按 (source, city) 精确限定，避免用一个 source
    的完整性去收敛另一个 source 的同名城市。

    ``monitored_pairs`` 是**配置里**的全部目标，和上面那个"本轮扫全了的"
    不是一回事——分片和节流会让正常监控的城市这轮不出现。它只用于孤儿路径：
    把已经彻底移出监控的城市里的鬼影 listing 也收敛掉。
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

    reserved_hours, occupied_hours = _stale_hours()
    return storage.mark_stale_listings(
        cities=complete_cities if complete_cities else None,
        source_city_pairs=complete_source_cities if complete_source_cities else None,
        monitored_pairs=monitored_pairs,
        orphan_days=orphan_days,
        reserved_hours=reserved_hours,
        occupied_hours=occupied_hours,
        full_lifecycle_sources=_full_lifecycle_sources(),
    )


def _full_lifecycle_sources() -> set[str]:
    """feed 已覆盖「已预留」状态的 source。读配置失败时返回空集。

    fail-open 到旧行为：拿不准就按「消失有歧义」处理，先推 Reserved。宁可多一站
    推测，也不要因为一次配置读取失败就把还在付款窗口里的房源直接判终态。
    """
    try:
        from config import load_config
        return set(load_config().sources_with_full_lifecycle())
    except Exception:
        logger.warning("读取 full_lifecycle_sources 失败，按旧判据收敛", exc_info=True)
        return set()


def _sweep_aging(storage: Storage, completeness: dict[str, bool]) -> int:
    """每轮跑一趟老化收敛（不含孤儿路径），返回收敛条数。

    为什么必须每轮跑
    ----------------
    孤儿那趟 24 小时一次。那个节奏对 30 天的宽限期是合适的，但会把小时级的
    老化窗口整个吃掉——房源满 2 小时该判终态了，实际要等到下一次 24 小时的
    整点，最坏挂 26 小时。**阈值调了不改节奏，等于没调。**

    每轮跑没有累积开销：到终态的行之后一律被 WHERE 排除，稳态下命中 0 行。
    **阈值本身就是节流器**，不需要再加一个间隔开关。

    抽成函数不是为了复用（只有一个调用点），是为了让**调用点本身**可测：
    这里最容易写错的是「顺手把 ``monitored_pairs`` 也传进去」，那会把扫全库
    的孤儿收敛拉进一条每轮都跑的路径上。那种错误在功能上看不出来——孤儿本来
    也该被收敛，只是不该由这一趟来做——只有盯着调用参数才能发现。
    """
    return _mark_stale_listings_for_complete_cities(storage, completeness)


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


def _merge_pending_events(
    storage: "Storage",
    new_listings: list["Listing"],
    status_changes: list[tuple["Listing", str, str]],
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


def _drop_shadow_sources(
    cfg,
    new_listings: list["Listing"],
    status_changes: list[tuple["Listing", str, str]],
    *,
    storage: "Storage | None" = None,
) -> tuple[list["Listing"], list[tuple["Listing", str, str]]]:
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
    """读一个正整数环境变量；空/非法/非正一律回落默认值。

    回落而不是抛错：一个手滑写错的降速阈值不该让整个监控起不来，而默认值本身
    是安全的那一侧（慢）。
    """
    try:
        val = int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default
    return val if val > 0 else default


_PERSONAL_PROXY_MIN_INTERVAL = _env_int_positive("PERSONAL_PROXY_MIN_INTERVAL", 120)
_H2S_SOURCE = "holland2stay"
_PREWARM_CANDIDATE_WAIT_SEC = 2.0  # 有真实候选时最多等 2s 让预登录赶上快速通道
_H2S_LOGIN_BLOCKED_SUPPRESS_SEC = 3600  # H2S 登录/预订 403 后 1 小时内不碰登录链路

#: 单个 source 一轮抓取的墙钟上限。见 _dispatch_scrape_tasks_async。
#:
#: 取 600 秒的依据：最慢的 source 是 Xior（实测每栋楼 13.9 秒，分片后每轮
#: 上限 8 栋 ≈ 112 秒），留出 5 倍余量。比 CHECK_INTERVAL(300) 大是有意的——
#: 这不是「该多久跑完」的目标，是「卡死了」的判据，宁可宽也不要误杀慢轮。
_SOURCE_DISPATCH_TIMEOUT_SEC = float(
    os.environ.get("SOURCE_DISPATCH_TIMEOUT_SEC") or "600")
_H2S_CIRCUIT_BASE_COOLDOWN = 1800  # H2S 抓取 403 后 30 分钟后做 canary
_H2S_CIRCUIT_MAX_COOLDOWN = 21600  # H2S 抓取连续 403 时最多暂停 6 小时
_H2S_LONG_BLOCK_STREAK = 3  # 第 3 次连续 H2S 403 起视为长时间被 block
_H2S_LONG_BLOCK_NOTIFY_INTERVAL = 21600  # 长时间 block admin 告警 6 小时最多一次

# 屏蔽通知节流：避免每轮抓取都给用户推一次相同的告警。
_BLOCK_NOTIFY_INTERVAL = 1800  # 30 分钟

# operation 被拒是个**只能靠改代码修好**的状态：不会自愈，也不随时间变化。
# 用 blocked 那 30 分钟的节奏去提醒，等于在人上班之前先发 16 条一模一样的告警。
# 6 小时一条足够——真正的紧迫感来自第一条。
_OPERATION_REJECTED_NOTIFY_INTERVAL = 21600  # 6 小时

# 维护通知节流：admin 已经在 dashboard banner 上看到维护态了，再叠加 web 通知
# 主要是让 admin 在收 push（如果接了）/ 刷通知面板时也能看到一条记录。
# 间隔比屏蔽长一截——维护态用户什么都做不了，没必要 30 min 一刷。
_MAINTENANCE_NOTIFY_INTERVAL = 3600  # 1 小时

# 这几个节流窗口全部**落库**：以前是模块级 float，注释写着「重启后清零，重启后
# 第一轮会再发通知，符合预期」——那在部署一天 12 次的节奏下就不符合预期了，
# 等于每次部署都给 admin 重发一遍同样的告警。见 mcore/backoff.py。
_throttle_notify_block = PersistedBackoff(
    "throttle_notify_block", max_seconds=_BLOCK_NOTIFY_INTERVAL)
_throttle_notify_operation_rejected = PersistedBackoff(
    "throttle_notify_operation_rejected", max_seconds=_OPERATION_REJECTED_NOTIFY_INTERVAL)
_throttle_h2s_long_block = PersistedBackoff(
    "throttle_h2s_long_block", max_seconds=_H2S_LONG_BLOCK_NOTIFY_INTERVAL)
_throttle_notify_maintenance = PersistedBackoff(
    "throttle_notify_maintenance", max_seconds=_MAINTENANCE_NOTIFY_INTERVAL)


#: 供 _should_notify_* 落库用的 Storage 句柄。由 main() 在启动时设一次。
#:
#: 为什么是模块级而不是参数：这几个 _should_notify_* 散布在十几个调用点，全都
#: 没有 storage 在手，逐个穿参数会把签名污染一大片。和 retry_queue / prewarm_cache
#: 一样按模块级单例处理。为 None 时节流退化成进程内——测试与 CLI 场景正是如此。
_throttle_storage_ref = None


def _throttle_storage():
    return _throttle_storage_ref


def _bind_persistent_state(storage) -> None:
    """把落库句柄接上，并从库里恢复所有退避 / 熔断 / 节流状态。

    在 main() 里、进入主循环之前调一次。没有这一步的话这些状态仍然只活在进程内
    ——正是本次要修的那个问题。
    """
    global _throttle_storage_ref
    _throttle_storage_ref = storage
    _source_circuits.load(storage)
    _source_pacing.load(storage)
    for b in (
        _h2s_login_block,
        _throttle_notify_block, _throttle_notify_operation_rejected,
        _throttle_h2s_long_block, _throttle_notify_maintenance,
        _throttle_notify_proxy, _throttle_notify_internal,
    ):
        try:
            b.load(storage)
        except Exception:
            logger.warning("恢复退避状态失败（按未退避处理）", exc_info=True)

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


def _should_notify_block() -> bool:
    """是否该发屏蔽通知。30 分钟最多一次，避免持续屏蔽时刷屏。"""
    return _throttle_notify_block.claim(_BLOCK_NOTIFY_INTERVAL, storage=_throttle_storage())


def _should_notify_operation_rejected() -> bool:
    """operation 被上游拒时是否该告警。6 小时最多一次，理由见常量注释。"""
    return _throttle_notify_operation_rejected.claim(_OPERATION_REJECTED_NOTIFY_INTERVAL, storage=_throttle_storage())


def _should_notify_h2s_long_block() -> bool:
    """H2S 长时间 403 后是否该通知 admin。6 小时最多一次。"""
    return _throttle_h2s_long_block.claim(_H2S_LONG_BLOCK_NOTIFY_INTERVAL, storage=_throttle_storage())


def _should_notify_maintenance() -> bool:
    """是否该给 admin 发维护通知。1 小时最多一次。"""
    return _throttle_notify_maintenance.claim(_MAINTENANCE_NOTIFY_INTERVAL, storage=_throttle_storage())

# 代理失效通知节流：代理挂了 admin 也只需要知道一次，30 min 一条够。
_PROXY_NOTIFY_INTERVAL = 1800  # 30 分钟
_throttle_notify_proxy = PersistedBackoff("throttle_notify_proxy", max_seconds=_PROXY_NOTIFY_INTERVAL)


def _should_notify_proxy() -> bool:
    """是否该给 admin 发代理失效通知。30 分钟最多一次。"""
    return _throttle_notify_proxy.claim(_PROXY_NOTIFY_INTERVAL, storage=_throttle_storage())

# 未分类/管线错误通知节流：某个反复抛错的内部异常（如 DB 锁死、磁盘满）若每轮
# 都通知会刷屏 admin。和代理/屏蔽一样 30 min 一条。
_INTERNAL_NOTIFY_INTERVAL = 1800  # 30 分钟
_throttle_notify_internal = PersistedBackoff("throttle_notify_internal", max_seconds=_INTERNAL_NOTIFY_INTERVAL)


def _should_notify_internal() -> bool:
    """是否该给 admin 发未分类/管线内部错误通知。30 分钟最多一次。"""
    return _throttle_notify_internal.claim(_INTERNAL_NOTIFY_INTERVAL, storage=_throttle_storage())

# ── 全面故障告警 ────────────────────────────────────────────────────
#
# 「某个 source 挂了」「完整率下滑」由 watchdog 负责，但 watchdog 只在**跑完一轮**
# 之后才评估。当所有 source 同时失败时 run_once 直接上抛，那一轮的 watchdog 根本
# 不会执行——最需要告警的场景反而最安静。
#
# 2026-08-05 04:24–09:29 代理断了 5 小时 5 分钟，59 轮全灭，admin 一条告警都没
# 收到：当时 main_loop 的 network / blocked / 内部异常分支只 logger.error。而
# run_once 的 ScrapeNetworkError 分支明确写着「让 main_loop 做连续失败计数和冷却」
# ——交接的另一头是空的。这里就是补上的那一头。
#
# 首次达阈值立即发，之后 15 / 30 / 60 分钟递增，封顶 1 小时：全面宕机要立刻知道，
# 但连续 5 小时不该收 20 条。
_OUTAGE_ALERT_BACKOFF = (900, 1800, 3600)


class _OutageTracker:
    """全面故障的告警节流与恢复判定。

    只做判定不做 IO，投递交给调用方——这样测试不必搭 push / web_notifier 环境。
    时间由调用方传入（``time.monotonic()``），测试可直接推进。
    """

    def __init__(self) -> None:
        self._started_at: float | None = None
        self._alerts = 0
        self._last_alert_at = 0.0
        self._rounds = 0

    @property
    def active(self) -> bool:
        return self._started_at is not None

    @property
    def rounds(self) -> int:
        """本次故障已经失败了多少轮。"""
        return self._rounds

    def elapsed(self, now: float) -> float:
        """本次故障已持续多少秒；不在故障中时为 0。"""
        return 0.0 if self._started_at is None else now - self._started_at

    def record_failure(self, now: float) -> bool:
        """记一次全面失败，返回本次是否该发告警。

        **首次必发。** 调用方只在确认是全面故障时才调进来（网络分支要连续 3 轮、
        屏蔽分支一轮就是 15 分钟起步的冷却），门槛已经在外面把住了；这里再压一层
        「先观察几轮」只会推迟通知。恢复告警靠这条性质成立：进入过故障态就一定
        通知过，不会出现凭空一条「已恢复」。
        """
        if self._started_at is None:
            self._started_at = now
            self._alerts = 0
            self._last_alert_at = 0.0
            self._rounds = 0
        self._rounds += 1
        if self._alerts:
            idx = min(self._alerts - 1, len(_OUTAGE_ALERT_BACKOFF) - 1)
            if now - self._last_alert_at < _OUTAGE_ALERT_BACKOFF[idx]:
                return False
        self._alerts += 1
        self._last_alert_at = now
        return True

    def record_success(self, now: float) -> tuple[float, int] | None:
        """记一次成功。返回 ``(故障持续秒数, 失败轮数)``；本来就没在故障中则 None。

        不在故障中时**必须**返回 None：否则每一轮正常抓取都会发一条「已恢复」。
        """
        if self._started_at is None:
            return None
        span, rounds = now - self._started_at, self._rounds
        self._started_at = None
        self._alerts = 0
        self._rounds = 0
        return span, rounds


#: 模块级单例。进程重启后清零——重启后若故障仍在，第一轮会重新告警，符合预期。
_outage = _OutageTracker()


def _format_duration(seconds: float) -> str:
    """把秒数写成「N 小时 M 分钟」。告警里「5 小时 5 分钟」比「18300 秒」有用。"""
    total = int(seconds)
    h, m = divmod(total // 60, 60)
    if h:
        return f"{h} 小时 {m} 分钟"
    return f"{m} 分钟" if m else f"{total} 秒"


async def _alert_outage(
    storage: "Storage",
    web_notifier: "WebNotifier | None",
    summary: str,
    detail: str,
) -> None:
    """全面故障告警。只发 admin——用户对代理/网络故障无从处置。"""
    msg = (
        f"⛔ 全面抓取故障：{summary}\n\n"
        f"已持续 {_format_duration(_outage.elapsed(time.monotonic()))}，"
        f"连续 {_outage.rounds} 轮所有 source 均失败。\n\n"
        f"{detail}\n\n"
        "监控进程仍在运行并按退避重试；恢复后会再发一条通知。"
    )
    await _notify_admin_only(storage, web_notifier, msg, kind="outage")


async def _alert_outage_recovered(
    storage: "Storage",
    web_notifier: "WebNotifier | None",
    span: float,
    rounds: int,
) -> None:
    """恢复告警。kind 与故障告警不同，否则会被 push 的 dedup 压掉。"""
    msg = (
        f"✅ 抓取已恢复\n\n"
        f"本次全面故障持续 {_format_duration(span)}，期间 {rounds} 轮全部失败。"
    )
    await _notify_admin_only(storage, web_notifier, msg, kind="outage_recovered")


def _h2s_login_suppressed_remaining() -> int:
    """H2S 登录/预订链路是否因 403 被临时抑制；返回剩余秒数。"""
    return _h2s_login_block.remaining()


def _mark_h2s_login_blocked(reason: BaseException | str, storage=None) -> None:
    """H2S 登录/预订遇到 Cloudflare 403 后，短期内停止触碰登录链路。

    ``storage`` 给了才落库。少数调用点（预订结果回调）拿不到 storage，那里退化
    成进程内抑制——比原来强，但不跨重启。
    """
    _h2s_login_block.open(
        _H2S_LOGIN_BLOCKED_SUPPRESS_SEC, reason=str(reason), storage=storage,
    )
    prewarm_cache.clear()
    logger.warning(
        "🚫 H2S 登录/预订遇到 Cloudflare WAF，未来 %d 秒内暂停登录链路: %s",
        _H2S_LOGIN_BLOCKED_SUPPRESS_SEC,
        reason,
    )


def _h2s_circuit_remaining() -> int:
    """H2S 抓取 circuit breaker 剩余暂停秒数。"""
    return _source_circuits.remaining(_H2S_SOURCE)


def _mark_h2s_scrape_blocked(reason: BaseException | str, storage=None) -> int:
    """H2S 抓取被 Cloudflare 403 后，打开熔断 + 抑制登录链路。

    熔断本身已经推广成 per-source（``mcore/circuit.py``），这里只剩 H2S 独有的
    那一半：**登录/预订链路抑制**。那是因为只有 H2S 有自动预订，而 403 之后继续
    去碰登录接口只会让 WAF 状态更热。
    """
    exc = reason if isinstance(reason, BaseException) else BlockedError(str(reason))
    cooldown = _source_circuits.trip(_H2S_SOURCE, exc, storage=storage)
    _mark_h2s_login_blocked(reason, storage=storage)
    return cooldown


def _mark_h2s_scrape_recovered(storage=None) -> None:
    """H2S canary 成功后关闭熔断 + 解除登录抑制，下一轮恢复完整 H2S 抓取。"""
    _source_circuits.recover(_H2S_SOURCE, storage=storage)
    _h2s_login_block.reset(storage)


# 需要浏览器传输层的 source。它们的 Playwright 对象绑定创建线程，因此**必须**
# 各自跑在 ``_get_browser_executor(source)`` 的专属长存单线程上；放到默认 executor
# 里会因线程漂移抛 ``greenlet.error: Cannot switch to a different thread``。
_BROWSER_SOURCES = frozenset({"holland2stay", "xior"})


# 全部 source 都失败时，挑一个最有代表性的异常上抛。顺序 = 「哪个根因更值得
# main_loop 据以决策」：
#   ProxyError    有明确修复动作（切备用代理 / 降级直连），而且全员失败时它
#                 大概率就是其它 source 失败的共同根因 —— 排最前
#   Maintenance   平台自己会恢复，安静长冷却、不告警用户
#   OperationNotAllowed
#                 唯一需要**人去改代码**的一条（照抄站点那条 operation）。
#                 排在 Blocked 前面：两者都是 403，但把后者当前者的代价实测过
#                 ——2026-08-19 一次自动预订连续两次「重建 CF 会话」各跑一轮
#                 完整挑战，75 秒、约 3 MB 代理流量，结束时还是同一个 403，
#                 随后误判触发 1 小时登录链路抑制。同一轮里两种 403 都出现时，
#                 「这条 operation 没登记」是更具体也更可诉诸行动的诊断。
#   Blocked       长冷却 + 指数退避
#   RateLimit     短冷却 + 自适应间隔翻倍
#   Network       连续失败计数
# ProxyError 是 ScrapeNetworkError 子类，必须排在它前面才匹配得到。
_ROUND_FAILURE_PRIORITY: tuple[type[BaseException], ...] = (
    ProxyError,
    UpstreamMaintenanceError,
    OperationNotAllowedError,
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
    mode, n = _source_circuits.plan(_H2S_SOURCE, n_tasks=len(h2s_tasks))
    if mode == "none":
        return [], "none"
    if mode == "open":
        logger.warning(
            "🚫 H2S source 熔断中，跳过 %d 个 H2S 任务，%d 秒后 canary。最近原因: %s",
            len(h2s_tasks),
            _source_circuits.remaining(_H2S_SOURCE),
            _source_circuits.reason(_H2S_SOURCE) or "unknown",
        )
        return [], "open"
    if mode == "canary":
        logger.warning(
            "🚫 H2S source 熔断到期，本轮只用 1 个城市做 canary: %s",
            h2s_tasks[0].city_display,
        )
    return h2s_tasks[:n], mode


_PID_FILE = DATA_DIR / "monitor.pid"
_RELOAD_REQUEST_FILE = DATA_DIR / "monitor.reload"

# 热重载事件（SIGHUP → 唤醒 main_loop 中的 sleep，立即重载配置）
_reload_event: asyncio.Event | None = None

# 类型别名：每个用户与其对应通知器的配对列表
UserNotifiers = list[tuple[UserConfig, BaseNotifier]]

# mcore 服务实例（进程生命周期内单例）
retry_queue = RetryQueue()
prewarm_cache = PrewarmCache()
#: H2S 熔断 / 登录抑制的退避状态。**落库，跨进程重启存活**——见 mcore/backoff.py。
#:
#: 这些以前是裸的模块级 float（基于 time.monotonic），重启即清零。也就是说正在被
#: CF 封、退避已拉到 6 小时的时候部署一次，立刻满速重打。2026-08-20 一天部署 12 次
#: = 12 次退避清零。
#: 每个 source 一个熔断器，策略按 source 配（mcore/circuit.py）。以前只有 H2S 有
#: ——而实测整源失败次数是 xior 147 / ourdomain 67 / ourcampus 60 / H2S 6，
#: 保护恰好装在最不需要它的那个上。
_source_circuits = SourceCircuits()

#: 按 429 历史自动伸缩各 source 的最小抓取间隔。和熔断分工不同：熔断管「出事
#: 之后停多久」，这个管「平时多久打一次」。只有熔断的话，冷却一结束节奏就回到
#: 原值，过三五十分钟再撞一次——生产实测每天约 10 次，从没停过。
_source_pacing = AdaptivePacing()
_h2s_login_block = PersistedBackoff(
    "h2s_login_block", max_seconds=_H2S_LOGIN_BLOCKED_SUPPRESS_SEC,
)


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
    storage: "Storage | None" = None,
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
                    _mark_h2s_login_blocked(e, storage)
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


#: 只有这个平台的抽签房源走聚合。写死而不是「所有 source」是因为判据是**平台
#: 行为**：只有 H2S 会把一批抽签房源在同一轮里一次性放出（2026-08-25 实测一轮
#: 9 套）。别的平台根本没有抽签状态，写宽了等于给一个不存在的情况留后门。
_BATCH_SOURCE = "holland2stay"

#: 攒够几套才聚合。1 套时聚合与逐条完全等价，却换来一条信息更少的消息
#: （聚合版不带类型/楼层/能耗），所以从 2 起。
_BATCH_MIN = 2


def _split_batchable(matched: list["Listing"]) -> tuple[list["Listing"], list["Listing"]]:
    """把某用户本轮的匹配拆成「聚合发」和「逐条发」两堆。

    只有 **H2S 的抽签房源**进第一堆，而且要够 ``_BATCH_MIN`` 套。

    为什么只聚合抽签
    ----------------
    进抽签池不是先到先得——晚看半小时不影响抽中概率。而「可直接预订」相反，
    2026-08-25 实测中位窗口 154 分钟、最短 4 分钟，那种房源少发一条就是少一次
    机会，绝不能为了省配额把它埋进摘要里。

    顺序保持原样返回，不排序：调用方的日志和消息都按这个顺序输出，而
    ``new_listings`` 的顺序来自抓取，重排只会让人对不上号。
    """
    from models import is_lottery_status

    batched, singles = [], []
    for l in matched:
        if (l.source or "") == _BATCH_SOURCE and is_lottery_status(l.status):
            batched.append(l)
        else:
            singles.append(l)
    if len(batched) < _BATCH_MIN:
        # 不够数：原样退回逐条，且要**保持原顺序**，不能把它们甩到队尾。
        return [], matched
    return batched, singles


def _report_source_proxy_failure(source: str, exc: BaseException) -> None:
    """把单个 source 的代理故障报给代理池（冷却 / 切换 / 降级）。

    和 ``run_once`` 外层那个 ``except ProxyError`` 处理的是同一件事，区别只在
    触发条件：那里要求整轮全灭，这里只要求「这次失败是代理造成的」。代理坏了
    就是坏了，与同轮别的 source 走没走运无关。

    刻意**不**在这里发 admin 告警：外层那条已经有 30 分钟节流，而这条每轮每
    source 都可能触发，加告警只会把真信号淹掉。这里只负责让冷却状态跟上事实。

    任何异常都吞掉：报告失败不该把「隔离这个 source、继续跑别的」这件事一起
    带崩——那正是这层隔离存在的理由。
    """
    from config import (
        is_proxy_in_cooldown,
        report_proxy_failure,
    )

    try:
        before = get_proxy_url()
        service_error = is_proxy_service_error(exc)
        account_level = is_proxy_account_error(exc)
        after = report_proxy_failure(
            service_error_confirmed=service_error, account_level=account_level,
        )
        if before and is_proxy_in_cooldown(before):
            logger.warning(
                "🛰️ [%s] 代理已确认故障并进入冷却%s：%s",
                source,
                "（账户级，长冷却）" if account_level else "",
                _redact_proxy_for_log(before),
            )
            if after != before:
                logger.info(
                    "🛰️ [%s] 下一轮改用 %s", source,
                    _redact_proxy_for_log(after) if after else "服务器原生 IP 直连",
                )
    except Exception:
        logger.debug("[%s] 上报代理故障失败（已忽略）", source, exc_info=True)


def _redact_proxy_for_log(url: str) -> str:
    """代理 URL 里带凭据，日志里只留 scheme + host:port。"""
    import re as _re
    return _re.sub(r"//[^@]*@", "//", url or "") or "(无)"


async def _notify_new_listings(
    new_listings: list["Listing"],
    user_notifiers: "UserNotifiers",
    web_notifier: "WebNotifier | None",
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
        for user, notifier in user_notifiers:
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
        batched, singles = _split_batchable(matched)
        if batched:
            logger.info(
                "[%s] %d 套 %s 抽签房源聚合成 1 条（逐条发会占掉当天邮件配额的一大半）",
                user.name, len(batched), _BATCH_SOURCE,
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
            result = await future
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
            elif result.phase == "operation_rejected":
                # 同样不是房源级问题，retry_queue 也保持不动。但**不调用
                # _mark_h2s_login_blocked**：那个函数假设「CF 盯上我们了，先躲
                # 一小时」，而这里 CF 什么都没做。躲一小时既救不了预订，还会把
                # 登录链路白白关掉——2026-08-19 就是这么从「预订坏了」变成
                # 「预订和登录都停了」的。
                pass
            else:
                for c in sorted_cands:
                    retry_queue.discard(user.id, c.id)

        if result.dry_run:
            logger.info("[%s] [DRY RUN] 自动预订跳过: %s", user.name, booked_listing.name)
        elif result.phase == "blocked":
            # 累积起来：admin 那条聚合成一份，用户那条按房源各发各的
            blocked_in_round.append((user, notifier, result.message, booked_listing))
        elif result.phase == "operation_rejected":
            rejected_in_round.append((user, notifier, result.message, booked_listing))
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
        if _should_notify_operation_rejected():
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

    # 代理全挂时只留下**直连打得通**的 source。放在节流和分片之前：被代理
    # 挡住的 source 本轮根本不该占分片游标。
    scrape_tasks = _apply_proxyless_gate(scrape_tasks)

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
        #: source → 本轮的熔断模式（normal / canary / open），canary 成功时据此
        #: 关闭熔断。
        source_modes: dict[str, str] = {}
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

        # completeness 的 key 直接用 dispatcher 那份共享实现
        # （``scrapers.completeness_key``）。这里以前有个本地 ``_ckey``，注释写着
        # 「与 scrapers._completeness_key 保持一致」——靠注释维持的一致性不是
        # 一致性，而形态一旦错开症状不是报错，是静默的错误收敛。

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
                # 打开这个 source 的熔断（按 source 各自的策略；不该熔断的异常
                # 会被 trip() 原样忽略）。以前只有 H2S 有这层保护——而实测里
                # xior 整源 429 失败 147 次、ourdomain / ourcampus 各 60+ 次，
                # H2S 总共才 6 次。保护装在了最不需要它的那个 source 上。
                _source_circuits.trip(src, e, storage=None if dry_run else storage)
                # 只有 429 才拉长节奏。403 / 网络错误 / 平台维护都不是「打得太
                # 勤」造成的，拉长间隔既治不了它们，又白白拖慢恢复。
                if isinstance(e, RateLimitError):
                    _source_pacing.penalize(src, storage=None if dry_run else storage)
                # 标 ✗ 而不是留空：完整扫描那行日志要看得出「这个 source 塌了」，
                # 且 False 会正确挡住该城市的 stale 收敛。
                for t in group:
                    completeness_all.setdefault(
                        completeness_key(src, t.city_display,
                                         multi_source=multi_source), False)
                logger.error(
                    "source %s 整体抓取失败，已隔离该 source（%d 个任务）: %s: %s",
                    src, len(group), type(e).__name__, e,
                    exc_info=True,
                )
                # 代理坏了要**当场报给代理池**，不能等整轮全灭。
                #
                # 这层跨源隔离接住异常之后就 return 了，而标记冷却/切换/降级的
                # 代码住在 run_once 外层的 `except ProxyError`——那里只有整轮
                # 所有 source 都失败时才够得着。于是形成一个死锁式的循环：
                #
                #   H2S 成功（浏览器建在好线路上，缓存 2 小时）
                #     → 整轮不算全灭 → 外层处理器不触发
                #     → 代理池永远不知道那个代理挂了
                #     → OurCampus / Xior 每轮现取代理，拿到没被冷却的死代理
                #     → 402，循环
                #
                # 2026-08-27 实测：08 时（本地）source 级隔离 176 次，真正报给
                # 代理池只有 4 次；同一小时 H2S 163/167 成功，而 OurCampus 和
                # Xior 是 0/167。**H2S 的健康恰恰是把另外两家钉死的原因**——
                # 它越顺，代理池越听不到坏消息。
                #
                # 判据也因此换掉了：冷却要回答的是「这个代理还能不能用」，而
                # 原先拿「整轮有没有全灭」来判，两者不是一回事。
                #
                # 误伤由既有的「连续两次确认」阈值挡住（_PROXY_FAILURE_CONFIRM_
                # THRESHOLD）：单次抖动只留一个 mark，不会真的冷却谁。
                if not dry_run and is_proxy_error(e):
                    _report_source_proxy_failure(src, e)
                if not dry_run:
                    _record_source_round(
                        storage, round_at=round_at, source=src,
                        targets=len(group), started_at=started_at, error=e,
                        total_targets=source_totals.get(src, len(group)),
                    )
                return
            succeeded_sources.append(src)
            if source_modes.get(src) == "canary":
                _source_circuits.recover(src, storage=None if dry_run else storage)
            # 干净地抓完一轮。攒够连续若干轮才会降一档——降太快会在限流阈值
            # 附近来回振荡，每振荡一次就要再付一次熔断冷却。
            #
            # canary 轮也算数：它只抓 1 个 target，是这个 source 当下能给出的
            # 最干净的证据。不算的话，熔断频繁的 source 永远攒不满计数，倍率
            # 只升不降——那正是它最需要降下来的时候。
            _source_pacing.relax(src, storage=None if dry_run else storage)
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
        # 每个用户都收到「监控将暂停 5 分钟」。24 小时内三次。
        for src in sorted({t.source for t in other_tasks}):
            group = [t for t in other_tasks if t.source == src]
            mode, n = _source_circuits.plan(src, n_tasks=len(group))
            source_modes[src] = mode
            if mode == "open":
                remaining = _source_circuits.remaining(src)
                logger.warning(
                    "🚫 source %s 熔断中，跳过 %d 个任务，%d 秒后做 canary。最近原因: %s",
                    src, len(group), remaining,
                    _source_circuits.reason(src) or "unknown",
                )
                # 熔断期完全跳过，但**要记一行遥测**：不记的话这个 source 在面板
                # 上直接消失，只能看到 last_round_at 不断变老——与「进程挂了」
                # 无从区分。记成一次带 CircuitOpen 标记的失败，让「按设计退避」
                # 本身可见。这条和 H2S 分支的处理是对齐的。
                for t in group:
                    completeness_all.setdefault(
                        completeness_key(src, t.city_display,
                                         multi_source=multi_source), False)
                if not dry_run:
                    _record_source_round(
                        storage, round_at=round_at, source=src,
                        targets=len(group),
                        total_targets=source_totals.get(src, len(group)),
                        error_type=CIRCUIT_OPEN_ERROR,
                        error_msg=_source_circuits.reason(src) or "circuit open",
                    )
                continue
            if mode == "canary":
                logger.warning(
                    "🚫 source %s 熔断到期，本轮只用 1 个 target 做 canary: %s",
                    src, group[0].city_display,
                )
                group = group[:n]
            await _dispatch_isolated(src, group)

        if selected_h2s:
            started_at = time.monotonic()
            try:
                fresh_part, completeness_part = await _dispatch(
                    selected_h2s, isolated=True, browser_source=_H2S_SOURCE
                )
            except BlockedError as e:
                h2s_blocked = e
                _mark_h2s_scrape_blocked(e, storage)
                if not dry_run:
                    _record_source_round(
                        storage, round_at=round_at, source=_H2S_SOURCE,
                        targets=len(selected_h2s), started_at=started_at, error=e,
                        total_targets=source_totals.get(_H2S_SOURCE, len(selected_h2s)),
                    )
            except Exception as e:
                # 与其它 source 同样只隔离，不上抛。
                #
                # 这里原本是 `raise`（注释写的是「按旧契约」——那个契约来自 H2S
                # 还是唯一 source 的年代）。上面那段 2026-08-03 的跨源隔离**把
                # H2S 漏在了外面**，于是它成了唯一能一票否决整轮的源：异常穿透
                # 到 run_once 的 except，同轮已经抓好的 OurDomain / OurCampus /
                # Xior 结果全部丢弃——不入库、不通知、不做状态变更。
                #
                # 2026-08-17 实测代价：H2S 端点迁移后返回 404，13.7 小时里
                # **118 轮无一轮走完**，而那三个平台每轮都抓成功了（Xior 464 次、
                # OurDomain 119 次、OurCampus 118 次），全部白抓。
                #
                # 该上抛的判定在下面：`source_failures and not succeeded_sources`
                # ——**所有** source 都失败才算整轮失败。H2S 喂进去即可，不必自己
                # 决定整轮的生死。403 熔断仍走上面的 BlockedError 分支，不受影响。
                source_failures.append((_H2S_SOURCE, e))
                # 下面两件与 _dispatch_isolated 的失败分支保持一致。**H2S 曾经
                # 两件都没做**：这段是 2026-08-17 把 H2S 从「一票否决整轮」改成
                # 「只隔离」时照着写的，只搬了隔离那一半。后果是
                #
                #   - 429 时不 penalize：H2S 的 pacing 倍率恒为 1.0，自适应节奏
                #     对它整个不生效（multiplier() 按 source 取，没有排除 H2S）；
                #   - 代理坏了不上报：而 H2S 又是**最不容易失败的那个 source**
                #     （浏览器建在好线路上、会话缓存两小时），于是它每成功一轮，
                #     整轮就不算全灭，外层那个 `except ProxyError` 就够不着——
                #     同一段注释在 _dispatch_isolated 里已经描述过这个死锁，
                #     只是当时没意识到 H2S 自己也在环里。
                if isinstance(e, RateLimitError):
                    _source_pacing.penalize(_H2S_SOURCE,
                                            storage=None if dry_run else storage)
                for t in selected_h2s:
                    completeness_all.setdefault(
                        completeness_key(_H2S_SOURCE, t.city_display,
                                         multi_source=multi_source), False)
                logger.error(
                    "source %s 整体抓取失败，已隔离该 source（%d 个任务）: %s: %s",
                    _H2S_SOURCE, len(selected_h2s), type(e).__name__, e,
                    exc_info=True,
                )
                if not dry_run and is_proxy_error(e):
                    _report_source_proxy_failure(_H2S_SOURCE, e)
                if not dry_run:
                    _record_source_round(
                        storage, round_at=round_at, source=_H2S_SOURCE,
                        targets=len(selected_h2s), started_at=started_at, error=e,
                        total_targets=source_totals.get(_H2S_SOURCE, len(selected_h2s)),
                    )
            else:
                succeeded_sources.append(_H2S_SOURCE)
                fresh_all.extend(fresh_part)
                completeness_all.update(completeness_part)
                if h2s_mode == "canary":
                    _mark_h2s_scrape_recovered(storage)
                # 与 _dispatch_isolated 的成功分支一致。只 penalize 不 relax 会让
                # 倍率只升不降，而 H2S 这边原本两个都没有——补上就要成对补，
                # 单补一个比一个都不补更糟。
                _source_pacing.relax(_H2S_SOURCE,
                                     storage=None if dry_run else storage)
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
                error_type=CIRCUIT_OPEN_ERROR,
                error_msg=_source_circuits.reason(_H2S_SOURCE) or "circuit open",
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
            except BlockedError as e:
                # 见上面 pre_fut.result() 处的注释：这里原本也是永远不会命中的
                # BookingBlockedError。OperationNotAllowedError 不该被接住。
                _mark_h2s_login_blocked(e, storage)
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
            except BlockedError as e:
                # 见上面 pre_fut.result() 处的注释：这里原本也是永远不会命中的
                # BookingBlockedError。OperationNotAllowedError 不该被接住。
                _mark_h2s_login_blocked(e, storage)
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
                await _notify_admin_only(
                    storage, web_notifier, err_msg, kind="h2s_circuit",
                )
            if (
                not dry_run
                and _source_circuits.fail_streak(_H2S_SOURCE) >= _H2S_LONG_BLOCK_STREAK
                and _should_notify_h2s_long_block()
            ):
                cooldown = _h2s_circuit_remaining()
                await _notify_admin_only(
                    storage,
                    web_notifier,
                    (
                        "H2S 长时间被 block，需要检查服务器\n\n"
                        f"连续 H2S 403: {_source_circuits.fail_streak(_H2S_SOURCE)} 次\n"
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
    except OperationNotAllowedError as e:
        # 403，但正文是上游应用说「这条 operation 没登记」，与出口 IP 无关。
        #
        # 以前这里没有分支，它会一路落到最底下的 except Exception，被报成
        # 「未分类的内部异常，请查看服务器日志排查」——**全系统最可诉诸行动的
        # 一条故障，却把排查引向了服务器日志。** 同一个类在 booker 里早就有专属
        # phase（operation_rejected），抓取侧一直没补上。
        #
        # 不换 IP、不熔断、不抑制登录：那三件事对这种 403 一件都不管用
        # （见 scrapers.base.OperationNotAllowedError）。上抛让 main_loop 做限流
        # 冷却，避免在一个必然失败的查询上每轮烧一次代理流量。
        await _stash_pending_prewarms()
        logger.error(
            "⛔ 上游拒绝了我们发的 GraphQL operation（HTTP 403，非 Cloudflare）"
            " cities=%d users=%d：%s",
            len(scrape_tasks), len(user_notifiers), e,
        )
        if not dry_run and _should_notify_internal():
            await _notify_admin_only(
                storage, web_notifier,
                f"⛔ 上游按 operation 白名单拒绝\n\n{e}\n\n"
                f"这不是 Cloudflare 屏蔽，也不是网络问题——换 IP、重建会话、"
                f"等冷却都无效。\n"
                f"唯一的修法是把站点自己发的那条 operation **原样照抄**回来"
                f"（步骤见 docs/H2S.md §5.1：钩住 crypto.subtle.encrypt 截获"
                f"加密前的明文）。\n"
                f"注意白名单按 operationName + 归一化字段集比对，删一个字段"
                f"就是 403。\n"
                f"30 分钟内不重复通知。",
                kind="error",
            )
        raise
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
            await _notify_admin_only(
                storage, web_notifier, err_msg, kind="scrape_blocked",
            )
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
            await _notify_admin_only(
                storage, web_notifier, err_msg, kind="scrape_rate_limited",
            )
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
        # 账户级（402 欠费 / 407 认证失败）走长冷却：那种故障不会自己好，
        # 按 10 分钟回去重试只是每小时自伤六次。
        account_level = is_proxy_account_error(e)
        new_proxy = report_proxy_failure(
            service_error_confirmed=service_error, account_level=account_level,
        )
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
            cfg, new_listings, status_changes, storage=storage,
        )

        # ── 重放上一轮没发出去的事件 ─────────────────────────────────── #
        # diff() 检测变更的副作用就是覆盖掉用来检测的旧状态，所以「diff 已提交、
        # 通知还没发」这个窗口里进程一死，事件就永久丢了——下一轮 diff 看到
        # old_status == new_status，什么都不产出。触发条件很日常：2026-08-20
        # 一天之内部署了 12 次，每次 --force-recreate 都在打断正在跑的轮次。
        #
        # 这里把 notified=0 的事件捞回来（有时间窗、有条数上限，见
        # mstorage/_listings.py），交付语义从 at-most-once 变成 at-least-once。
        #
        # **重复通知只是打扰，漏掉通知会让人错过房子** —— 代价不对称，所以选前者。
        if not dry_run:
            try:
                replayed = _merge_pending_events(
                    storage, new_listings, status_changes,
                )
                if replayed:
                    logger.warning(
                        "重放 %d 条上一轮未发出的通知事件"
                        "（上次多半是部署或崩溃打断了通知阶段）", replayed,
                    )
                storage.retire_stale_pending()
            except Exception:
                # 重放是补偿机制，它自己坏掉不该拖垮正常通知
                logger.warning("未投递事件重放失败（本轮跳过）", exc_info=True)

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
            storage=storage,
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

            # 成功：高峰期将自适应间隔缩短 5%（逐步逼近 min_interval）
            if is_peak:
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

def _validate_structured_config() -> None:
    """校验监控范围那批「塞在字符串里的表格」，把问题吼出来。

    面板保存时已经挡过一道，但值还能从别处来：手工改库、迁移、环境变量覆盖、
    从旧版本升上来。这里是最后一道。

    **不阻断启动。** 一个配置笔误让整个监控停摆，代价远大于笔误本身；何况多数
    问题只影响一个平台，其余照跑更有价值。但要打 ERROR，让它进 errors.log 和
    /monitoring 面板。
    """
    try:
        from target_config import validate_effective

        problems = validate_effective()
    except Exception:
        logger.debug("结构化配置自检失败（已忽略）", exc_info=True)
        return

    for p in problems:
        if p.fatal:
            logger.error("⚙️  配置格式有误：%s", p)
        else:
            logger.warning("⚙️  配置提示：%s", p)


def _bootstrap_settings() -> None:
    """把 runtime 类配置从 app_settings 表注入 os.environ；首次运行顺带做迁移。

    必须在 ``load_config()`` 之前调用——后者读的就是 ``os.environ``。

    整段吞异常：配置存储出问题时全部回落到 .env / 代码默认值继续跑。让监控因为
    读不出配置而起不来，代价远大于用一轮旧配置。
    """
    try:
        from config import DB_PATH, TIMEZONE
        from settings_store import env_overrides, hydrate, migrate_env_to_db
        from storage import Storage

        st = Storage(DB_PATH, timezone_str=TIMEZONE)
        try:
            migrate_env_to_db(st, ENV_PATH)
            n = hydrate(st)
            if n:
                logger.info("已从 app_settings 载入 %d 项运维配置", n)
            # 迁移之后 .env 里不该再有 runtime 键。有就是被手工加回来了——它会
            # 盖过面板，而面板不会有任何提示，改了没反应会让人以为是坏了。
            leftover = env_overrides(ENV_PATH)
            if leftover:
                logger.warning(
                    "⚙️  .env 里这些键会盖过面板设置（面板改了不生效）：%s。"
                    "确认要用面板管理的话，从 .env 删掉即可",
                    ", ".join(leftover),
                )
            _validate_structured_config()
        finally:
            st.close()

    except Exception:
        logger.warning("载入 app_settings 失败，本次使用 .env / 默认值", exc_info=True)


def _reload_settings() -> None:
    """热重载时重新注水。

    ``load_dotenv(override=True)`` 会把 .env 重放一遍，而 runtime 配置早已不在
    .env 里；不重新注水的话，面板刚改的值这一程都不会生效。
    """
    try:
        from config import DB_PATH, TIMEZONE
        from settings_store import hydrate
        from storage import Storage

        st = Storage(DB_PATH, timezone_str=TIMEZONE)
        try:
            hydrate(st)
        finally:
            st.close()
    except Exception:
        logger.warning("热重载时读取 app_settings 失败，沿用当前值", exc_info=True)


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
