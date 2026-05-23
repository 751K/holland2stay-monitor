"""
scrapers/base.py — 抓取层抽象（source-agnostic）
==================================================

P0 引入：把抓取从 H2S 单源耦合中解放出来，每个第三方租房平台实现
``AbstractScraper`` 子类，注册到 ``scrapers/__init__.py:SCRAPER_REGISTRY``。

设计要点
--------
- **同步 API**：保留现有 sync 范式，monitor 那边继续用 ``run_in_executor``
  把抓取放进线程池。改 async 是另一坨工作量，不在 P0 范围内。
- **零回归承诺**：仅 Holland2Stay 一家时行为完全不变——`HollandStayScraper`
  内部直接调原 `scraper.py:scrape_all`，I/O 形状一致。
- **异常分类**：``RateLimitError`` / ``BlockedError`` / ``ScrapeNetworkError``
  都来自这里。P0 之前它们住在 `scraper.py`；现在挪到中性位置，旧
  `scraper.py` 仅做 re-export 保持 import 路径兼容。
- **数据模型保守演进**：Listing 在 P0 里只新增 `source` 字段（默认
  ``"holland2stay"``），id / native_id 的前缀化迁移留到 P1（接 OurDomain
  时一起做，避免提前改 status_changes / web_notifications / iOS deep
  link 的 listing_id 引用）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from models import Listing


# ────────────────────────────────────────────────────────────────────
# 异常分类（原来住在 scraper.py，挪到中性位置便于多 scraper 复用）
# ────────────────────────────────────────────────────────────────────

class RateLimitError(Exception):
    """
    抓取目标 API 持续返回 429 Too Many Requests，所有重试均已耗尽。

    由各 scraper 在 HTTP 层抛出，monitor.main_loop 捕获并触发冷却期
    （比正常 interval 长，避免持续刷 429）。
    """


class BlockedError(Exception):
    """
    抓取目标 API 返回 403 — 通常是 Cloudflare WAF 屏蔽。

    与 429 的区别
    -------------
    429 = "请求太快，等等就好"，退避后通常自动恢复。
    403 = "我们不想服务你"，等待不会自动恢复，需要换代理 / 重启 / 冷却。

    monitor 那边对 403 比 429 更长的 cooldown（15 min）并节流通知。
    """


class ScrapeNetworkError(Exception):
    """
    抓取过程中遭遇网络错误（连接超时、TLS 中断、DNS 失败等），
    非 API 层错误——换代理 / 检查网络即可恢复。

    与 RateLimitError / BlockedError 的区别
    ---------------------------------------
    - RateLimitError → API 说"太快"（429），退避后可自动恢复
    - BlockedError   → API 说"不服务你"（403），等待无法恢复
    - ScrapeNetworkError → 根本没拿到 API 响应——代理挂了、网络断了、DNS 故障

    由 scraper 在第一页网络失败时抛出，经 monitor 上层做连续失败计数
    并在超过阈值后冷却。
    """


class UpstreamMaintenanceError(Exception):
    """
    抓取目标平台正在做计划内维护（主站显示"We'll be back soon" /
    "scheduled maintenance"），整个站 + API 都暂时不可用。

    与 BlockedError 的区别
    ---------------------
    - BlockedError → Cloudflare WAF 主动拒绝服务，**永远不会**自己恢复，
      必须换代理 / 重启 / 等几小时；要给用户发告警让其介入。
    - UpstreamMaintenanceError → 对方运维窗口，**自己**会恢复（公告通常
      1–2 小时窗口），不需要用户做任何事，只需要 monitor 安静等待。

    monitor 那边对维护态的处理：长冷却（15 min）、INFO 而非 ERROR 日志、
    不发用户告警（避免凌晨维护把人吵醒），状态上抛 web dashboard
    显示一个温和的 banner。
    """


# ────────────────────────────────────────────────────────────────────
# 共享常量与工具
# ────────────────────────────────────────────────────────────────────

# 429 退避策略：依次等待这些秒数后重试。
# 两次重试 = 最多额外等待 90 秒后才放弃并抛出 RateLimitError。
RATE_LIMIT_BACKOFF: tuple[int, ...] = (30, 60)


def is_cloudflare_body(body: str) -> bool:
    """判断 HTTP 403 响应体是否为 Cloudflare challenge 页面。"""
    lower = body.lower()
    return (
        "cloudflare" in lower
        or "no-js ie6 oldie" in body
        or "challenge-platform" in lower
        or "<!doctype html>" in lower[:80]
    )


# 维护页关键词（大小写不敏感匹配）。
# Holland2Stay 在计划维护期间会把整站换成一个简单 HTML，含以下短语之一。
# 抽成常量便于复用 + 测试时 monkeypatch 注入"假维护页"。
_MAINTENANCE_MARKERS: tuple[str, ...] = (
    "we'll be back soon",
    "we will be back soon",
    "scheduled maintenance",
    "performing scheduled maintenance",
    "currently performing scheduled",
)


def is_maintenance_body(body: str) -> bool:
    """
    判断响应体（HTML 字符串）是否为"平台维护中"占位页。

    判定基于多个英文短语任一命中——H2S 维护页是固定模板，命中率高。
    对短 body / JSON 不会误伤（这些字符串不会出现在正常 GraphQL 响应里）。
    """
    if not body:
        return False
    lower = body.lower()
    return any(marker in lower for marker in _MAINTENANCE_MARKERS)


# 主站探测 URL：维护时这个 URL 直接返回维护 HTML（200 或 503 都可能），
# 不走 Cloudflare WAF，所以即便 GraphQL 端点被 403，主站也能看到真正状态。
H2S_MAIN_SITE_URL = "https://www.holland2stay.com/"


def probe_h2s_maintenance(session, *, timeout: float = 10.0) -> bool:
    """
    用现有 curl_cffi Session GET 主站，看是否命中维护页。

    用法
    ----
    连续 N 次 403 时（每次 403 = 一轮抓取被拒），调一次本函数。
    True  → 抛 UpstreamMaintenanceError，让 monitor 走长冷却 + 安静等。
    False → 维持原来的 BlockedError 路径，按 Cloudflare 屏蔽处理。

    异常安全
    --------
    探测本身的网络异常一律吞掉，返回 False——探测失败不应该升级成更严重
    的错误，让上层继续按 Block 路径走即可。
    """
    try:
        resp = session.get(H2S_MAIN_SITE_URL, timeout=timeout)
    except Exception:
        return False
    body = getattr(resp, "text", "") or ""
    # 即便 status_code 是 503，body 里照样含 "We'll be back soon" — 不限制 status。
    return is_maintenance_body(body[:4000])


# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ScrapeTask:
    """
    一次抓取的最小单位。source-agnostic。

    由 ``Config.scrape_tasks_v2()`` 产出，每个 ScrapeTask 描述 "在某个
    source 上抓某个城市"。``city_key`` 是平台内部的城市标识（H2S 是
    数字 city_id 字符串，OurDomain 可能是 slug），``city_display`` 是
    用户可见的城市名，最终写入 ``Listing.city`` 字段。

    单平台 / 多平台都按这个抽象走——H2S 多个城市 = 多个 ScrapeTask；
    将来 OurDomain 加进来 = 多 N 个 ScrapeTask，归 OurDomainScraper 处理。
    """
    source: str          # "holland2stay" / "ourdomain" / "duwo" ...
    city_key: str        # 平台内部城市标识（H2S 的 city_id_str 等）
    city_display: str    # 用户可见城市名（写进 Listing.city）
    # 扩展位：每个平台可能有自己的 filter 字段（例如 H2S 的 availability_ids）。
    # 放 dict 而非具体类型，避免基类感知子类细节。
    extra: dict = field(default_factory=dict)


@dataclass
class ScrapeResult:
    """
    一次抓取任务的产出。

    ``complete`` 字段非常关键：只有完整扫描完所有页 + 解析失败率达标的
    城市，monitor 才会对它执行 stale listing 收敛（避免抓不全时误把
    存量 listing 标记成 Occupied）。
    """
    task: ScrapeTask
    listings: list[Listing]
    complete: bool       # 全部页都抓完 + 总数 sanity 检查通过 = True
    error: Optional[str] = None


# ────────────────────────────────────────────────────────────────────
# Scraper 抽象基类
# ────────────────────────────────────────────────────────────────────

class AbstractScraper(ABC):
    """
    每个第三方租房平台实现一个子类，注册到 ``SCRAPER_REGISTRY``。

    子类约定
    --------
    - 必须设 ``source: str`` 类属性（与 SCRAPER_REGISTRY key 一致）
    - 必须实现 ``scrape(task) -> ScrapeResult`` 同步方法
    - 可选实现 ``prewarm_session()`` / ``try_book(listing)`` 钩子；
      多数平台不支持 booking → 留空即可（基类 no-op 默认实现）

    线程模型
    --------
    scrape() 在 monitor 的 executor 线程里跑，每个 scraper 实例可能被
    多个线程并发使用——内部状态（session / cookie）需自行加锁或每次
    新建。Holland2Stay 用的就是"每次新建 Session"策略，无需锁。
    """

    # 子类必须覆盖。例：``source = "holland2stay"``
    source: str = ""

    @abstractmethod
    def scrape(self, task: ScrapeTask) -> ScrapeResult:
        """
        抓取单个 ScrapeTask（典型粒度：一个 source × 一个城市）。

        异常协议
        --------
        - 第 1 页网络失败 → 抛 ScrapeNetworkError（让 monitor 计入连续失败）
        - 持续 429 → 抛 RateLimitError
        - 403 Cloudflare → 抛 BlockedError
        - 其他单页失败、解析失败 → 不抛异常，返回部分结果 + complete=False

        Returns
        -------
        ScrapeResult，listings 列表里每个 Listing 的 ``source`` 字段应已
        填好为 ``self.source``。
        """

    # ── 可选钩子（多数平台不实现，留空 no-op 默认即可）───────────────

    def prewarm_session(self) -> None:
        """登录 / 预热会话。仅 H2S 等支持 auto-book 的平台需要。"""
        return None

    def try_book(self, listing: Listing) -> bool:
        """自动预订单条 listing。仅 H2S 等支持的平台实现。"""
        return False
