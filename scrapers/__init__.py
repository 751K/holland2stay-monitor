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
    RateLimitError,
    ScrapeNetworkError,
    ScrapeResult,
    ScrapeTask,
    is_cloudflare_body,
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


def get_scraper(source: str) -> Optional[AbstractScraper]:
    """根据 source 名取一个 scraper 实例；未注册返回 None。"""
    cls = SCRAPER_REGISTRY.get(source)
    return cls() if cls else None


def dispatch_scrape_tasks(
    tasks: list[ScrapeTask],
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

    P0 现状
    -------
    monitor.run_once 当前仍走 ``scraper.scrape_all(city_tasks, availability_ids)``
    旧路径——零回归。本函数留作 P1 接 OurDomain 时把 monitor 切过来用。
    单源情况下两个路径行为完全一致。
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
    hard_failures: list[tuple[str, Exception]] = []

    for source, source_tasks in by_source.items():
        scraper = get_scraper(source)
        if not scraper:
            # 未注册的 source 不抛异常，跳过——避免某条配置笔误把整个监控卡住
            for t in source_tasks:
                completeness[_completeness_key(source, t.city_display, by_source)] = False
            continue

        for t in source_tasks:
            ckey = _completeness_key(source, t.city_display, by_source)
            try:
                result = scraper.scrape(t)
                success_count += 1
                all_listings.extend(result.listings)
                completeness[ckey] = result.complete
            except (RateLimitError, BlockedError) as e:
                hard_failures.append((ckey, e))
                completeness[ckey] = False
                logger.error("%s 抓取被限流/屏蔽，已隔离该任务: %s", ckey, e)
            except ScrapeNetworkError as e:
                network_failures.append(ckey)
                logger.error("%s 抓取网络失败，已隔离该任务: %s", ckey, e)
                # 单 city 网络失败不进 completeness（与现有 scrape_all 行为一致）

    # 429 / 403：若没有任何任务成功，维持旧行为让 monitor 进入冷却；若已有
    # 其它平台成功，则返回部分结果，避免 OurDomain 被挡时拖垮 H2S。
    if success_count == 0 and hard_failures:
        raise hard_failures[0][1]

    # 全部任务都网络失败 → 上抛，让 monitor 做连续失败计数
    if success_count == 0 and network_failures and len(network_failures) == len(tasks):
        raise ScrapeNetworkError(
            f"全部 {len(tasks)} 个任务网络失败: {', '.join(network_failures)}"
        )

    return all_listings, completeness


def _completeness_key(
    source: str,
    city_display: str,
    by_source: dict[str, list[ScrapeTask]],
) -> str:
    """
    多源时 completeness 字典 key 加 source 前缀防同名城市覆盖。
    单源时退化为纯 city_display（保持与旧 scrape_all 输出兼容）。
    """
    if len(by_source) <= 1:
        return city_display
    return f"{source}:{city_display}"


__all__ = [
    "RATE_LIMIT_BACKOFF",
    "AbstractScraper",
    "BlockedError",
    "HollandStayScraper",
    "OurDomainScraper",
    "XiorScraper",
    "RateLimitError",
    "SCRAPER_REGISTRY",
    "ScrapeNetworkError",
    "ScrapeResult",
    "ScrapeTask",
    "dispatch_scrape_tasks",
    "get_scraper",
    "is_cloudflare_body",
]
