"""
scrapers — 多源抓取层
=====================

每个第三方租房平台一个子模块，实现 ``AbstractScraper``。
``SCRAPER_REGISTRY`` 映射 source 名（与 ``Listing.source`` / `.env`
``SOURCES`` 一致）到实现类，``monitor`` / `scraper` 通过它来 dispatch。

公开 API
--------
- ``base`` 子模块：抽象基类 + 异常 + ScrapeTask / ScrapeResult
- ``SCRAPER_REGISTRY``：所有已注册的 scraper 实现
- ``get_scraper(source)``：根据 source 名取实现实例（缺失返回 None）

新增 scraper 步骤
-----------------
1. 在 ``scrapers/{name}.py`` 里实现 ``AbstractScraper`` 子类
2. 在本文件底部 import 并加入 ``SCRAPER_REGISTRY``
3. 在 ``.env:SOURCES`` 里把它打开

P0 阶段只有 holland2stay 一家——保持现网行为不变。
"""
from __future__ import annotations

import logging
from typing import Optional

from .base import (
    RATE_LIMIT_BACKOFF,
    AbstractScraper,
    BlockedError,
    ProxyError,
    RateLimitError,
    ScrapeNetworkError,
    ScrapeResult,
    ScrapeTask,
    UpstreamMaintenanceError,
    is_cloudflare_body,
    is_maintenance_body,
    is_proxy_error,
    is_proxy_service_error,
)
from .holland2stay import HollandStayScraper
from .ourdomain import OurDomainScraper
from .xior import XiorScraper


logger = logging.getLogger(__name__)

SCRAPER_REGISTRY: dict[str, type[AbstractScraper]] = {
    cls.source: cls for cls in [
        HollandStayScraper,
        OurDomainScraper,
        XiorScraper,
    ]
}


# source → (注册的类, 实例)。跨轮复用，见 get_scraper 的说明。
# 连类一起存：注册表被替换时（测试里常见）实例要跟着换，否则会拿到用旧类
# 建的对象。
_SCRAPER_INSTANCES: dict[str, tuple[type, AbstractScraper]] = {}


def reset_scraper_instances() -> None:
    """丢弃所有缓存实例（H2S 的浏览器随实例一起被丢弃）。

    给测试用——保证用例之间不会共享 scraper 状态。
    """
    _SCRAPER_INSTANCES.clear()


def get_scraper(source: str) -> Optional[AbstractScraper]:
    """根据 source 名取 scraper 实例；未注册返回 None。

    实例按 source 缓存并**跨轮复用**。H2S 的浏览器挂在实例上，每轮新建实例
    会让 ``HollandStayScraper`` 的跨轮复用逻辑永远命不中，退化成每轮重建浏览
    器 + 完整重过一次 CF 挑战。

    线程安全：每个浏览器型 source 恒定跑在**自己**的专用长存线程里（monitor
    ``_get_browser_executor(source)``，每 source 一条——两个 Playwright sync
    实例不能共存于同一线程），dispatcher 又是逐 source 串行调用，所以缓存实例
    不会导致 Playwright 对象被跨线程使用。
    """
    cls = SCRAPER_REGISTRY.get(source)
    if cls is None:
        return None
    cached = _SCRAPER_INSTANCES.get(source)
    if cached is not None and cached[0] is cls:
        return cached[1]
    instance = cls()
    _SCRAPER_INSTANCES[source] = (cls, instance)
    return instance


def _safe_invalidate(scraper: AbstractScraper, label: str) -> None:
    """丢弃 scraper 的长生命周期资源；失效动作本身不该再把 dispatcher 带崩。"""
    try:
        scraper.invalidate_session()
    except Exception:
        logger.debug("%s invalidate_session 失败（已忽略）", label, exc_info=True)


def dispatch_scrape_tasks(
    tasks: list[ScrapeTask],
    *,
    multi_source: bool = False,
) -> tuple[list, dict[str, bool]]:
    """
    P0 多源 dispatcher：按 source 分组、按注册表查实例、逐 task scrape，
    把各 source 的产出合并成 ``(all_listings, completeness)`` 兼容旧形状。

    职责
    ----
    - 单 source 内的多 city 串行调用同一实例（保留现有 H2S Session 模型）
    - 跨 source 用 try/except 隔离：一个 source 挂了不影响其他 source；
      只有所有启用任务都失败时才上抛给 monitor 做冷却
    - ``RateLimitError`` / ``BlockedError`` 在单 source 或全 source 失败时
      继续上抛，保留与 monitor.main_loop 的冷却契约兼容
    - ``ScrapeNetworkError`` 累积，若全部 source 都网络失败则上抛
    - ``completeness`` 字典 key 是 ``city_display``——多 source 同名城市
      （例如 H2S 的 Amsterdam + OurDomain 的 Amsterdam）会前缀化 source
      避免覆盖：``"holland2stay:Amsterdam"`` / ``"ourdomain:Amsterdam"``

    Parameters
    ----------
    multi_source
        本轮**整体**是否跨多个 source。必须由调用方给，不能靠 ``tasks`` 自己
        推断：monitor 是按 source 分开调用本函数的，每次调用里 ``by_source``
        恒为 1，于是前缀永远加不上——防同名覆盖的机制形同虚设，且
        ``mark_stale_listings`` 会退化成不带 source 条件的 ``city IN (...)``，
        用一个 source 的完整性去收敛另一个 source 的 listing。

    调用方
    ------
    ``monitor.run_once`` 走本函数（``scrape_tasks_v2()`` → 本 dispatcher），
    是当前唯一的生产抓取路径。旧的 ``scraper.scrape_all`` 已删除。
    """
    from collections import defaultdict

    # 延迟 import 避免循环
    from models import Listing  # noqa: F401  (用于类型提示)

    by_source: dict[str, list[ScrapeTask]] = defaultdict(list)
    for t in tasks:
        by_source[t.source].append(t)

    all_listings: list = []
    completeness: dict[str, bool] = {}
    success_count = 0
    network_failures: list[str] = []
    proxy_failure: Optional[ProxyError] = None   # 任一任务遇到代理故障就记下
    hard_failures: list[tuple[str, Exception]] = []

    for source, source_tasks in by_source.items():
        scraper = get_scraper(source)
        if not scraper:
            # 未注册的 source 不抛异常，跳过——避免某条配置笔误把整个监控卡住
            for t in source_tasks:
                completeness[_completeness_key(source, t.city_display, by_source, multi_source)] = False
            continue

        # batch_session() 让 scraper 把 Session/TLS 指纹提升到批次级——H2S
        # 这样一批城市只握手一次、用同一个指纹（恢复 P0 之前 scrape_all 的
        # 行为）。默认 no-op，OurDomain 等仍各 task 自管会话。
        #
        # 整个 with 再包一层 try：``batch_session()`` 的进入/退出发生在下面那圈
        # per-task try **之外**，它抛的异常（浏览器创建失败、CF 挑战没过、
        # Playwright 崩溃）会直接穿透整个 dispatcher，把同轮里已经抓好的其它
        # source 结果一起带走——正是 per-task 隔离想避免的事。

        # 本批次里是否出现过 403。出现过就在批次结束后丢掉该 source 的长生命
        # 周期资源（浏览器），下轮重建——被 CF 标记的会话留着只会一直 403。
        source_blocked: Optional[BlockedError] = None

        try:
            with scraper.batch_session():
                for t in source_tasks:
                    ckey = _completeness_key(source, t.city_display, by_source, multi_source)
                    try:
                        result = scraper.scrape(t)
                        success_count += 1
                        all_listings.extend(result.listings)
                        completeness[ckey] = result.complete
                    except UpstreamMaintenanceError as e:
                        # 平台维护是站点级状态——不和其它 source 隔离尝试也无意义，
                        # 但仍然走 hard_failures 计数：让"全部任务都失败"判定成立时
                        # 直接上抛维护异常，monitor 据此走长冷却 + 安静等。
                        hard_failures.append((ckey, e))
                        completeness[ckey] = False
                        logger.info("%s 平台维护中，已隔离该任务: %s", ckey, e)
                    except (RateLimitError, BlockedError) as e:
                        hard_failures.append((ckey, e))
                        completeness[ckey] = False
                        # 403 记下来批次结束后丢会话；429 不丢——「等等就好」，
                        # 重建只是白白多过一次 CF 挑战。
                        if isinstance(e, BlockedError) and source_blocked is None:
                            source_blocked = e
                        logger.error("%s 抓取被限流/屏蔽，已隔离该任务: %s", ckey, e)
                    except (KeyboardInterrupt, SystemExit):
                        raise
                    except ScrapeNetworkError as e:
                        network_failures.append(ckey)
                        # ProxyError 是 ScrapeNetworkError 子类——单独记下，便于全失败
                        # 时上抛 ProxyError 让 monitor 发"代理失效"告警。
                        if isinstance(e, ProxyError) and proxy_failure is None:
                            proxy_failure = e
                        logger.error("%s 抓取网络失败，已隔离该任务: %s", ckey, e)
                        # 单 city 网络失败不进 completeness（与现有 scrape_all 行为一致）
                    except Exception as e:
                        # 未预期的异常同样要**按 task 隔离**。此前它会穿透整个
                        # dispatcher，把同轮里已经抓好的其它 source 结果一起带走
                        # ——2026-08-02 实测：Xior 的 greenlet.error 导致 H2S 和
                        # OurDomain 的结果全部丢失，整轮无完整扫描城市。
                        #
                        # 这类异常通常意味着底层会话已不可用，留着会让后续每轮
                        # 重复失败，所以顺带丢弃该 source 的长生命周期资源。
                        hard_failures.append((ckey, e))
                        completeness[ckey] = False
                        logger.error(
                            "%s 抓取出现未预期异常，已隔离该任务: %s: %s",
                            ckey, type(e).__name__, e, exc_info=True,
                        )
                        _safe_invalidate(scraper, ckey)

            # 403 后丢会话，**批次结束后**才丢：批次中间丢的话，同 source 的
            # 后续 task 会各自触发一次浏览器重建（每次都是一轮完整 CF 挑战，
            # 失败还会连锁重试），一栋楼的 403 能把整批拖成分钟级。
            if source_blocked is not None:
                logger.warning(
                    "%s 本批次遭遇 403，丢弃该 source 的浏览器/会话，下轮重建: %s",
                    source, source_blocked,
                )
                _safe_invalidate(scraper, source)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:
            # 批次会话本身失败（浏览器建不起来 / CF 挑战没过 / Playwright 崩）。
            # 只补还没有结论的 task——异常若来自 ``__exit__``，前面已经跑完的
            # task 有自己的 completeness，不该被这里覆盖。
            for t in source_tasks:
                ckey = _completeness_key(source, t.city_display, by_source, multi_source)
                if ckey in completeness:
                    continue
                completeness[ckey] = False
                hard_failures.append((ckey, e))
            logger.error(
                "%s 批次会话失败，已隔离该 source: %s: %s",
                source, type(e).__name__, e, exc_info=True,
            )
            # 无条件丢：能走到这里就说明批次会话的建立或收尾出了问题，
            # 留着可疑会话的代价远大于下轮多一次冷启动。
            _safe_invalidate(scraper, source)

    # 429 / 403 / 维护：若没有任何任务成功，维持旧行为让 monitor 进入冷却；
    # 若已有其它平台成功，则返回部分结果，避免 OurDomain 被挡时拖垮 H2S。
    # 全失败时优先上抛 UpstreamMaintenanceError——它是最有用的信号，monitor
    # 据此能选择"长冷却 + 不通知"而非"长冷却 + 通知"。
    if success_count == 0 and hard_failures:
        maint = next(
            (e for _, e in hard_failures if isinstance(e, UpstreamMaintenanceError)),
            None,
        )
        raise maint if maint is not None else hard_failures[0][1]

    # 全部任务都网络失败 → 上抛，让 monitor 做连续失败计数。
    # 若失败是代理故障，上抛 ProxyError（ScrapeNetworkError 子类，控制流不变），
    # monitor 据此额外发"代理失效"告警。
    if success_count == 0 and network_failures and len(network_failures) == len(tasks):
        if proxy_failure is not None:
            raise ProxyError(
                f"全部 {len(tasks)} 个任务因代理故障失败: {', '.join(network_failures)}"
            ) from proxy_failure
        raise ScrapeNetworkError(
            f"全部 {len(tasks)} 个任务网络失败: {', '.join(network_failures)}"
        )

    return all_listings, completeness


def _completeness_key(
    source: str,
    city_display: str,
    by_source: dict[str, list[ScrapeTask]],
    multi_source: bool = False,
) -> str:
    """
    多源时 completeness 字典 key 加 source 前缀防同名城市覆盖。
    单源时退化为纯 city_display（保持与旧 scrape_all 输出兼容）。

    ``by_source`` 只看得到**本次调用**的 source，而 monitor 是按 source 分开
    调用 dispatcher 的——光看它永远是 1。所以还要 ``multi_source``：由知道
    整轮全貌的调用方给出。
    """
    if multi_source or len(by_source) > 1:
        return f"{source}:{city_display}"
    return city_display


__all__ = [
    "RATE_LIMIT_BACKOFF",
    "AbstractScraper",
    "BlockedError",
    "HollandStayScraper",
    "OurDomainScraper",
    "XiorScraper",
    "ProxyError",
    "RateLimitError",
    "SCRAPER_REGISTRY",
    "ScrapeNetworkError",
    "ScrapeResult",
    "ScrapeTask",
    "UpstreamMaintenanceError",
    "dispatch_scrape_tasks",
    "get_scraper",
    "is_cloudflare_body",
    "is_maintenance_body",
    "is_proxy_error",
    "is_proxy_service_error",
]
