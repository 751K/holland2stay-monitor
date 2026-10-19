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

from config import get_proxy_url
from mcore.health import CIRCUIT_OPEN_ERROR
from mcore.h2s_guard import (
    H2S_SOURCE,
    mark_h2s_scrape_blocked,
    mark_h2s_scrape_recovered,
)
from mcore.sources import BROWSER_SOURCES
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

def get_browser_executor(source: str) -> ThreadPoolExecutor:
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

    浏览器型 source 各自走专属的长存线程（见 ``get_browser_executor``）；
    非浏览器 source 继续用默认 executor。

    ``multi_source`` 要透传给 dispatcher：本函数每次只喂一个 source 的任务，
    dispatcher 自己看不出整轮是不是多源（见 ``scrapers.completeness_key``）。
    """
    executor = (
        get_browser_executor(browser_source or H2S_SOURCE) if isolated else None
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


def split_h2s_tasks(tasks) -> tuple[list, list]:
    """拆分 H2S 与其它 source 任务，便于只熔断 H2S。"""
    h2s_tasks = [t for t in tasks if t.source == H2S_SOURCE]
    other_tasks = [t for t in tasks if t.source != H2S_SOURCE]
    return other_tasks, h2s_tasks


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


async def dispatch_with_h2s_circuit(
    loop, tasks, *, storage, source_totals, multi_source, dry_run,
):
    other_tasks, h2s_tasks = split_h2s_tasks(tasks)
    selected_h2s, h2s_mode = select_h2s_tasks_for_circuit(h2s_tasks)

    fresh_all: list[Listing] = []
    completeness_all: dict[str, bool] = {}
    #: source → 本轮的熔断模式（normal / canary / open），canary 成功时据此
    #: 关闭熔断。
    source_modes: dict[str, str] = {}
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

    # completeness 的 key 直接用 dispatcher 那份共享实现
    # （``scrapers.completeness_key``）。这里以前有个本地 ``_ckey``，注释写着
    # 「与 scrapers._completeness_key 保持一致」——靠注释维持的一致性不是
    # 一致性，而形态一旦错开症状不是报错，是静默的错误收敛。

    async def _dispatch(selected, *, isolated: bool = False, browser_source: str = ""):
        result = await dispatch_scrape_tasks_async(
            loop,
            selected,
            isolated=isolated,
            browser_source=browser_source,
            multi_source=multi_source,
        )
        return unpack_scrape_result(result)

    async def _dispatch_isolated(src: str, group: list) -> None:
        """跑一个 source 的全部任务，失败只影响这个 source。

        浏览器型 source（H2S / Xior）必须绑到各自的长存单线程
        （见 ``get_browser_executor``）：Playwright 的对象绑定创建线程，
        默认 executor 的线程不固定，跨线程调用会抛
        ``greenlet.error: Cannot switch to a different thread``。
        """
        started_at = time.monotonic()
        try:
            fresh_part, completeness_part = await _dispatch(
                group,
                isolated=src in BROWSER_SOURCES,
                browser_source=src,
            )
        except Exception as e:
            source_failures.append((src, e))
            # 打开这个 source 的熔断（按 source 各自的策略；不该熔断的异常
            # 会被 trip() 原样忽略）。以前只有 H2S 有这层保护——而实测里
            # xior 整源 429 失败 147 次、ourdomain / ourcampus 各 60+ 次，
            # H2S 总共才 6 次。保护装在了最不需要它的那个 source 上。
            runtime.source_circuits.trip(src, e, storage=None if dry_run else storage)
            # 只有 429 才拉长节奏。403 / 网络错误 / 平台维护都不是「打得太
            # 勤」造成的，拉长间隔既治不了它们，又白白拖慢恢复。
            if isinstance(e, RateLimitError):
                runtime.source_pacing.penalize(src, storage=None if dry_run else storage)
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
                report_source_proxy_failure(src, e)
            if not dry_run:
                record_source_round(
                    storage, round_at=round_at, source=src,
                    targets=len(group), started_at=started_at, error=e,
                    total_targets=source_totals.get(src, len(group)),
                )
            return
        succeeded_sources.append(src)
        if source_modes.get(src) == "canary":
            runtime.source_circuits.recover(src, storage=None if dry_run else storage)
        # 干净地抓完一轮。攒够连续若干轮才会降一档——降太快会在限流阈值
        # 附近来回振荡，每振荡一次就要再付一次熔断冷却。
        #
        # canary 轮也算数：它只抓 1 个 target，是这个 source 当下能给出的
        # 最干净的证据。不算的话，熔断频繁的 source 永远攒不满计数，倍率
        # 只升不降——那正是它最需要降下来的时候。
        runtime.source_pacing.relax(src, storage=None if dry_run else storage)
        fresh_all.extend(fresh_part)
        completeness_all.update(completeness_part)
        if not dry_run:
            complete_n, total_n = completeness_stats(completeness_part)
            record_source_round(
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
        mode, n = runtime.source_circuits.plan(src, n_tasks=len(group))
        source_modes[src] = mode
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
        await _dispatch_isolated(src, group)

    if selected_h2s:
        started_at = time.monotonic()
        try:
            fresh_part, completeness_part = await _dispatch(
                selected_h2s, isolated=True, browser_source=H2S_SOURCE
            )
        except BlockedError as e:
            h2s_blocked = e
            mark_h2s_scrape_blocked(e, storage)
            if not dry_run:
                record_source_round(
                    storage, round_at=round_at, source=H2S_SOURCE,
                    targets=len(selected_h2s), started_at=started_at, error=e,
                    total_targets=source_totals.get(H2S_SOURCE, len(selected_h2s)),
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
            source_failures.append((H2S_SOURCE, e))
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
                runtime.source_pacing.penalize(H2S_SOURCE,
                                        storage=None if dry_run else storage)
            for t in selected_h2s:
                completeness_all.setdefault(
                    completeness_key(H2S_SOURCE, t.city_display,
                                     multi_source=multi_source), False)
            logger.error(
                "source %s 整体抓取失败，已隔离该 source（%d 个任务）: %s: %s",
                H2S_SOURCE, len(selected_h2s), type(e).__name__, e,
                exc_info=True,
            )
            if not dry_run and is_proxy_error(e):
                report_source_proxy_failure(H2S_SOURCE, e)
            if not dry_run:
                record_source_round(
                    storage, round_at=round_at, source=H2S_SOURCE,
                    targets=len(selected_h2s), started_at=started_at, error=e,
                    total_targets=source_totals.get(H2S_SOURCE, len(selected_h2s)),
                )
        else:
            succeeded_sources.append(H2S_SOURCE)
            fresh_all.extend(fresh_part)
            completeness_all.update(completeness_part)
            if h2s_mode == "canary":
                mark_h2s_scrape_recovered(storage)
            # 与 _dispatch_isolated 的成功分支一致。只 penalize 不 relax 会让
            # 倍率只升不降，而 H2S 这边原本两个都没有——补上就要成对补，
            # 单补一个比一个都不补更糟。
            runtime.source_pacing.relax(H2S_SOURCE,
                                 storage=None if dry_run else storage)
            if not dry_run:
                complete_n, total_n = completeness_stats(completeness_part)
                record_source_round(
                    storage, round_at=round_at, source=H2S_SOURCE,
                    listings=len(fresh_part),
                    targets=total_n or len(selected_h2s), complete=complete_n,
                    started_at=started_at,
                    total_targets=source_totals.get(H2S_SOURCE, len(selected_h2s)),
                )
    elif h2s_tasks and h2s_mode == "open" and not dry_run:
        # 熔断期完全跳过 H2S。不记这一行的话，遥测里 H2S 会直接消失，面板
        # 只能看到 last_round_at 不断变老——与「进程挂了」无从区分。
        # 记成一次带 CircuitOpen 标记的失败，让「按设计退避」本身可见。
        record_source_round(
            storage, round_at=round_at, source=H2S_SOURCE,
            targets=len(h2s_tasks),
            total_targets=source_totals.get(H2S_SOURCE, len(h2s_tasks)),
            error_type=CIRCUIT_OPEN_ERROR,
            error_msg=runtime.source_circuits.reason(H2S_SOURCE) or "circuit open",
        )

    # 一个都没成功 = 本轮没有任何可用数据，维持旧契约上抛，让 main_loop 走
    # 冷却而不是原速空转。h2s_blocked 不在此列——H2S 熔断本身就是退避，且
    # run_once 对它有专门的分支。
    if source_failures and not succeeded_sources and h2s_blocked is None:
        raise pick_round_failure(source_failures)

    return fresh_all, completeness_all, h2s_blocked, h2s_mode
