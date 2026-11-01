"""抓取阶段：dispatch、按 source 隔离、熔断与 canary。

从 monitor.py 拆出。``run_once`` 只负责准备任务清单、调用这里、处理上抛的
异常。日志通道仍用 ``monitor``。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import NamedTuple
from dataclasses import dataclass, field

from config import get_proxy_url
from mcore.health import CIRCUIT_OPEN_ERROR
from mcore.h2s_guard import (
    H2S_SOURCE,
    mark_h2s_scrape_blocked,
    mark_h2s_scrape_recovered,
)
from mcore.sources import BROWSER_SOURCES
from mcore.browser_worker import BrowserWorker
from mcore.state import runtime
from mcore.telemetry import completeness_stats, record_source_round
from models import Listing
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

logger = logging.getLogger("monitor")

#: 单个 source 一轮抓取的墙钟上限。见 dispatch_scrape_tasks_async。
_SOURCE_DISPATCH_TIMEOUT_SEC = float(
    os.environ.get("SOURCE_DISPATCH_TIMEOUT_SEC") or "600")
_BROWSER_CLEANUP_GRACE_SEC = 60.0


def get_browser_worker(source: str) -> BrowserWorker:
    worker = runtime.browser_workers.get(source)
    if worker is None:
        worker = BrowserWorker(source)
        runtime.browser_workers[source] = worker
    return worker


def close_browser_workers() -> None:
    for source, worker in list(runtime.browser_workers.items()):
        try:
            worker.close()
        except Exception:
            logger.exception("无法回收 %s 浏览器工作进程", source)
        else:
            runtime.browser_workers.pop(source, None)


def get_browser_executor(source: str) -> ThreadPoolExecutor:
    """Each browser source has a single thread for synchronous worker IPC.

    Playwright itself runs in the source's persistent subprocess, never on this
    thread. Serializing IPC keeps one request in flight per worker.
    """
    ex = runtime.browser_executors.get(source)
    if ex is None:
        ex = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=f"{source}-scrape"
        )
        runtime.browser_executors[source] = ex
    return ex


async def dispatch_scrape_tasks_async(
    loop: asyncio.AbstractEventLoop,
    selected: list,
    *,
    isolated: bool = False,
    browser_source: str = "",
    multi_source: bool = False,
):
    """
    Run the synchronous scraper dispatcher from async monitor code.

    浏览器型 source 的抓取器在独立进程中运行，专属线程只负责进程通信；
    非浏览器 source 继续用默认 executor。

    ``multi_source`` 要透传给 dispatcher：本函数每次只喂一个 source 的任务，
    dispatcher 自己看不出整轮是不是多源（见 ``scrapers.completeness_key``）。
    """
    source = browser_source or H2S_SOURCE
    executor = get_browser_executor(source) if isolated else None
    if isolated:
        worker = get_browser_worker(source)
        job = lambda: worker.execute(  # noqa: E731
            selected, multi_source=multi_source, timeout=_SOURCE_DISPATCH_TIMEOUT_SEC,
        )
    else:
        job = lambda: dispatch_scrape_tasks(selected, multi_source=multi_source)  # noqa: E731
    from settings_store import context_job
    fut = loop.run_in_executor(
        executor, context_job(job),
    )
    # 墙钟兜底。**没有它的话，渲染器一卡就是整个监控停摆**：
    # ``page.evaluate`` 在 Playwright 里根本没有 timeout 参数（它等的是 JS
    # promise，``set_default_timeout`` 也管不到），``page.content()`` 同样。
    # 一个 wedged 页面会让浏览器线程永远停在那里，而这里的 await 没有上限，
    # run_once 就不返回了——表现是 last_round_at 不断变老，与「进程挂了」无从区分。
    #
    # 浏览器 worker 自己守截止时间并回收进程树；额外时间留给进程回收。
    try:
        timeout = (_SOURCE_DISPATCH_TIMEOUT_SEC + _BROWSER_CLEANUP_GRACE_SEC
                   if isolated else _SOURCE_DISPATCH_TIMEOUT_SEC)
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError as e:
        src = browser_source or (selected[0].source if selected else "?")
        if isolated:
            try:
                await asyncio.to_thread(worker.stop)
            except Exception:
                logger.exception("source %s 超时后无法回收浏览器工作进程", src)
        logger.error(
            "source %s 抓取超过 %.0f 秒没返回，判为卡死并放弃本轮"
            "（%d 个任务）",
            src, _SOURCE_DISPATCH_TIMEOUT_SEC, len(selected),
        )
        raise ScrapeNetworkError(
            f"{src} 抓取超时（>{_SOURCE_DISPATCH_TIMEOUT_SEC:.0f}s），疑似渲染器卡死"
        ) from e


def unpack_scrape_result(result):
    """兼容测试/旧 monkeypatch 返回 list；真实 scraper 返回 (listings, completeness)。"""
    if (
        isinstance(result, tuple)
        and len(result) == 2
        and isinstance(result[1], dict)
    ):
        return result
    return result, {}


#: 全部 source 都失败时，挑一个最有代表性的异常上抛。顺序 = 「哪个根因更值得
#: main_loop 据以决策」：ProxyError → Maintenance → OperationNotAllowed →
#: Blocked → RateLimit → Network。ProxyError 是 ScrapeNetworkError 子类，必须
#: 排在它前面才匹配得到。
_ROUND_FAILURE_PRIORITY: tuple[type[BaseException], ...] = (
    ProxyError,
    UpstreamMaintenanceError,
    OperationNotAllowedError,
    BlockedError,
    RateLimitError,
    ScrapeNetworkError,
)


def pick_round_failure(failures: list[tuple[str, Exception]]) -> Exception:
    """从各 source 的失败里挑一个上抛给 main_loop。"""
    for cls in _ROUND_FAILURE_PRIORITY:
        for _, exc in failures:
            if isinstance(exc, cls):
                return exc
    return failures[0][1]


async def dispatch_diagnostic(tasks: list):
    """Run the read-only CLI diagnostic through the same bounded workers."""
    grouped: dict[str, list] = {}
    for task in tasks:
        grouped.setdefault(task.source, []).append(task)
    loop = asyncio.get_running_loop()
    fresh, completeness, failures = [], {}, []
    successes = 0
    for source, group in grouped.items():
        try:
            result = await dispatch_scrape_tasks_async(
                loop, group, isolated=source in BROWSER_SOURCES,
                browser_source=source, multi_source=len(grouped) > 1,
            )
            listings, partial = unpack_scrape_result(result)
            fresh.extend(listings)
            completeness.update(partial)
            successes += 1
        except Exception as exc:
            failures.append((source, exc))
            for task in group:
                completeness[completeness_key(source, task.city_display,
                                               multi_source=len(grouped) > 1)] = False
            logger.warning("诊断抓取 %s 失败: %s", source, exc)
    if failures and not successes:
        raise pick_round_failure(failures)
    return fresh, completeness


def select_h2s_tasks_for_circuit(h2s_tasks: list) -> tuple[list, str]:
    """
    根据 H2S circuit 状态选择本轮 H2S 任务。

    Returns
    -------
    (selected_tasks, mode)
      normal  : circuit 未打开，抓全部 H2S 城市
      open    : circuit 冷却中，不抓 H2S
      canary  : 冷却到期，只抓 1 个 H2S 城市探测恢复
    """
    mode, n = runtime.source_circuits.plan(H2S_SOURCE, n_tasks=len(h2s_tasks))
    if mode == "none":
        return [], "none"
    if mode == "open":
        logger.warning(
            "🚫 H2S source 熔断中，跳过 %d 个 H2S 任务，%d 秒后 canary。最近原因: %s",
            len(h2s_tasks),
            runtime.source_circuits.remaining(H2S_SOURCE),
            runtime.source_circuits.reason(H2S_SOURCE) or "unknown",
        )
        return [], "open"
    if mode == "canary":
        logger.warning(
            "🚫 H2S source 熔断到期，本轮只用 1 个城市做 canary: %s",
            h2s_tasks[0].city_display,
        )
    return h2s_tasks[:n], mode


def report_source_proxy_failure(source: str, exc: BaseException) -> None:
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
                redact_proxy_for_log(before),
            )
            if after != before:
                logger.info(
                    "🛰️ [%s] 下一轮改用 %s", source,
                    redact_proxy_for_log(after) if after else "服务器原生 IP 直连",
                )
    except Exception:
        logger.debug("[%s] 上报代理故障失败（已忽略）", source, exc_info=True)


def redact_proxy_for_log(url: str) -> str:
    """代理 URL 里带凭据，日志里只留 scheme + host:port。"""
    import re as _re
    return _re.sub(r"//[^@]*@", "//", url or "") or "(无)"


class ScrapeRoundResult(NamedTuple):
    """One monitoring round's data, completeness and H2S circuit outcome."""
    listings: list[Listing]
    completeness: dict[str, bool]
    h2s_blocked: BlockedError | None
    h2s_mode: str


@dataclass(frozen=True)
class SourceDispatchContext:
    loop: asyncio.AbstractEventLoop
    storage: object
    source_totals: dict[str, int]
    multi_source: bool
    dry_run: bool
    round_at: str


@dataclass
class SourceDispatchResult:
    source: str
    listings: list[Listing] = field(default_factory=list)
    completeness: dict[str, bool] = field(default_factory=dict)
    error: Exception | None = None
    h2s_blocked: BlockedError | None = None


async def _dispatch_source(
    src: str, group: list, mode: str, context: SourceDispatchContext,
) -> SourceDispatchResult:
    """隔离单个平台；返回该平台结果，熔断、节流和代理仍由主进程管理。"""
    storage, dry_run = context.storage, context.dry_run
    started_at = time.monotonic()
    try:
        raw = await dispatch_scrape_tasks_async(
            context.loop, group, isolated=src in BROWSER_SOURCES,
            browser_source=src, multi_source=context.multi_source,
        )
        fresh_part, completeness_part = unpack_scrape_result(raw)
    except Exception as exc:
        if src == H2S_SOURCE and isinstance(exc, BlockedError):
            mark_h2s_scrape_blocked(exc, storage)
            if not dry_run:
                record_source_round(
                    storage, round_at=context.round_at, source=src,
                    targets=len(group), started_at=started_at, error=exc,
                    total_targets=context.source_totals.get(src, len(group)),
                )
            return SourceDispatchResult(src, h2s_blocked=exc)
        runtime.source_circuits.trip(src, exc, storage=None if dry_run else storage)
        if isinstance(exc, RateLimitError):
            runtime.source_pacing.penalize(src, storage=None if dry_run else storage)
        completeness = {
            completeness_key(src, task.city_display, multi_source=context.multi_source): False
            for task in group
        }
        logger.error(
            "source %s 整体抓取失败，已隔离该 source（%d 个任务）: %s: %s",
            src, len(group), type(exc).__name__, exc, exc_info=True,
        )
        if not dry_run and is_proxy_error(exc):
            report_source_proxy_failure(src, exc)
        if not dry_run:
            record_source_round(
                storage, round_at=context.round_at, source=src,
                targets=len(group), started_at=started_at, error=exc,
                total_targets=context.source_totals.get(src, len(group)),
            )
        return SourceDispatchResult(src, completeness=completeness, error=exc)

    if mode == "canary":
        if src == H2S_SOURCE:
            mark_h2s_scrape_recovered(storage)
        else:
            runtime.source_circuits.recover(src, storage=None if dry_run else storage)
    runtime.source_pacing.relax(src, storage=None if dry_run else storage)
    if not dry_run:
        complete_n, total_n = completeness_stats(completeness_part)
        record_source_round(
            storage, round_at=context.round_at, source=src,
            listings=len(fresh_part), targets=total_n or len(group), complete=complete_n,
            started_at=started_at, total_targets=context.source_totals.get(src, len(group)),
        )
    return SourceDispatchResult(src, fresh_part, completeness_part)


async def dispatch_with_h2s_circuit(
    loop, tasks, *, storage, source_totals, multi_source, dry_run,
):
    tasks_by_source: dict[str, list] = {}
    for task in tasks:
        tasks_by_source.setdefault(task.source, []).append(task)
    h2s_tasks = tasks_by_source.get(H2S_SOURCE, [])
    selected_h2s, h2s_mode = select_h2s_tasks_for_circuit(h2s_tasks)

    fresh_all: list[Listing] = []
    completeness_all: dict[str, bool] = {}
    #: source → 本轮的熔断模式（normal / canary / open），canary 成功时据此
    #: 关闭熔断。
    h2s_blocked: BlockedError | None = None
    source_failures: list[tuple[str, Exception]] = []
    succeeded_sources: list[str] = []

    # 整轮是否跨多个 source：用外层按配置算好的 multi_source。dispatcher
    # 自己看不出来——它每次只收到一个 source 的任务，所以 completeness key
    # 的 source 前缀得靠这个开关。也不能用这里的 tasks 现算：熔断、节流、
    # 只抓 H2S 都会让它变少，前缀一消失，同一批 listing 的 key 形态就在两轮
    # 之间跳变。

    # 整轮共用一个时间戳，各 source 的遥测行靠它分组。用 dispatch 开始的
    # 时刻而不是各 source 各自的完成时刻——否则同一轮的行会散成好几组。
    round_at = datetime.now(timezone.utc).isoformat()
    context = SourceDispatchContext(loop, storage, source_totals, multi_source, dry_run, round_at)

    # completeness 的 key 直接用 dispatcher 那份共享实现
    # （``scrapers.completeness_key``）。这里以前有个本地 ``_ckey``，注释写着
    # 「与 scrapers._completeness_key 保持一致」——靠注释维持的一致性不是
    # 一致性，而形态一旦错开症状不是报错，是静默的错误收敛。

    # 逐 source 隔离。dispatcher 内部已经按 task 隔离，但它在「本次调用的
    # 任务全部失败」时仍会上抛——而 monitor 是按 source 分开调用它的，于是
    # 那个判定退化成了「单个 source 全失败」，等于没有跨 source 保护。
    #
    # 2026-08-03 实测：Xior 四栋楼连续 429 让 RateLimitError 逃出整个
    # dispatch，同轮 OurDomain 已抓到的结果被丢弃、H2S 排在后面根本没执行、
    # 每个用户都收到「监控将暂停 5 分钟」。24 小时内三次。
    # 保留现有顺序（其他 source 先跑，H2S 最后），但共用调度和结果处理。
    source_order = sorted(source for source in tasks_by_source if source != H2S_SOURCE)
    if h2s_tasks:
        source_order.append(H2S_SOURCE)
    for src in source_order:
        group = tasks_by_source[src]
        if src == H2S_SOURCE:
            mode, n = h2s_mode, len(selected_h2s)
        else:
            mode, n = runtime.source_circuits.plan(src, n_tasks=len(group))
        if mode == "open":
            remaining = runtime.source_circuits.remaining(src)
            logger.warning(
                "🚫 source %s 熔断中，跳过 %d 个任务，%d 秒后做 canary。最近原因: %s",
                src, len(group), remaining,
                runtime.source_circuits.reason(src) or "unknown",
            )
            # 熔断期完全跳过，但**要记一行遥测**：不记的话这个 source 在面板
            # 上直接消失，只能看到 last_round_at 不断变老——与「进程挂了」
            # 无从区分。记成一次带 CircuitOpen 标记的失败，让「按设计退避」
            # 本身可见。这条和 H2S 分支的处理是对齐的。
            if src != H2S_SOURCE:
                for t in group:
                    completeness_all.setdefault(
                        completeness_key(src, t.city_display,
                                         multi_source=multi_source), False)
            if not dry_run:
                record_source_round(
                    storage, round_at=round_at, source=src,
                    targets=len(group),
                    total_targets=source_totals.get(src, len(group)),
                    error_type=CIRCUIT_OPEN_ERROR,
                    error_msg=runtime.source_circuits.reason(src) or "circuit open",
                )
            continue
        if mode == "canary":
            logger.warning(
                "🚫 source %s 熔断到期，本轮只用 1 个 target 做 canary: %s",
                src, group[0].city_display,
            )
            group = group[:n]
        result = await _dispatch_source(src, group, mode, context)
        fresh_all.extend(result.listings)
        if result.error is not None:
            for key, complete in result.completeness.items():
                completeness_all.setdefault(key, complete)
            source_failures.append((src, result.error))
        elif result.h2s_blocked is not None:
            h2s_blocked = result.h2s_blocked
        else:
            completeness_all.update(result.completeness)
            succeeded_sources.append(src)

    # 一个都没成功 = 本轮没有任何可用数据，维持旧契约上抛，让 main_loop 走
    # 冷却而不是原速空转。h2s_blocked 不在此列——H2S 熔断本身就是退避，且
    # run_once 对它有专门的分支。
    if source_failures and not succeeded_sources and h2s_blocked is None:
        raise pick_round_failure(source_failures)

    return ScrapeRoundResult(fresh_all, completeness_all, h2s_blocked, h2s_mode)
