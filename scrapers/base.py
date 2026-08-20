"""
scrapers/base.py — 抓取层抽象（source-agnostic）
==================================================

P0 引入：把抓取从 H2S 单源耦合中解放出来，每个第三方租房平台实现
``AbstractScraper`` 子类，注册到 ``scrapers/__init__.py:SCRAPER_REGISTRY``。

设计要点
--------
- **同步 API**：保留现有 sync 范式，monitor 那边继续用 ``run_in_executor``
  把抓取放进线程池。改 async 是另一坨工作量，不在 P0 范围内。
- **零回归承诺**：仅 Holland2Stay 一家时行为完全不变——多城市编排归
  dispatcher，I/O 形状一致。
- **异常分类**：``RateLimitError`` / ``BlockedError`` / ``ScrapeNetworkError``
  都来自这里。P0 之前它们住在顶层 `scraper.py`；那个模块迁移后只剩 re-export，
  生产代码零 import，2026-08-20 删除。
- **数据模型保守演进**：Listing 在 P0 里只新增 `source` 字段（默认
  ``"holland2stay"``），id / native_id 的前缀化迁移留到 P1（接 OurDomain
  时一起做，避免提前改 status_changes / web_notifications / iOS deep
  link 的 listing_id 引用）。
"""
from __future__ import annotations

import re as _re
from abc import ABC, abstractmethod
from contextlib import contextmanager
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


class ProxyError(ScrapeNetworkError):
    """
    抓取代理（``HTTPS_PROXY`` / ``ALL_PROXY``）本身故障——CONNECT 502 /
    隧道建立失败 / 连接被拒等。**注意是代理层挂了，不是 H2S 挂了。**

    继承 ``ScrapeNetworkError``：沿用"网络失败"的连续计数 + 冷却路径，
    无需改 monitor 的现有控制流。但 monitor 会 ``isinstance`` 出它来额外
    发一条专门的"代理失效"admin 告警——之前代理 502 只会默默进网络冷却，
    dashboard 不报警，运维以为"服务器崩了"却看不出根因。
    """


#: Chromium 对任何代理层失败给出的错误码。它不透出代理的真实状态码，但**码本身
#: 已经说明这是代理层的事**——浏览器根本没连上目标站点。
#:
#: 单独列出来是因为下面那几条 libcurl 文案是空格分词的，而 Chromium 用下划线：
#: ``ERR_TUNNEL_CONNECTION_FAILED`` 里没有 "tunnel connection failed"。
#: 2026-08-05 代理欠费停服 5 小时期间，浏览器侧的失败因此全部漏判为普通网络错误，
#: 代理冷却 / 切备用 / 降级直连三条路一条都没走。
_CHROMIUM_PROXY_ERROR_CODES: tuple[str, ...] = (
    "err_tunnel_connection_failed",
    "err_proxy_connection_failed",
    "err_proxy_auth_unsupported",
    "err_proxy_certificate_invalid",
    "err_no_supported_proxies",
)


def is_proxy_error(exc: BaseException) -> bool:
    """
    判断异常是否为抓取代理层故障。

    三类来源：

    - curl_cffi 代理失败抛 ``curl_cffi.requests.exceptions.ProxyError``（类名含
      Proxy）；
    - 底层 libcurl 的代理错误 message 含 "CONNECT tunnel failed" / "Proxy
      CONNECT" / curl 错误码 (56)（隧道失败）/ (97)（代理握手）；
    - Playwright/Chromium 给的 ``net::ERR_*`` 代理错误码。

    只回答「是不是代理层的问题」。够不够格让整条代理进冷却是
    ``is_proxy_service_error`` 的事。
    """
    name = type(exc).__name__.lower()
    if "proxy" in name:
        return True
    msg = _exception_chain_text(exc)
    if any(code in msg for code in _CHROMIUM_PROXY_ERROR_CODES):
        return True
    # config.probe_proxy() 的判词。它是直接跟代理握手得来的结论，比任何字符串
    # 特征都硬。这里认的是它的文案，改文案会让判定失效——
    # tests/test_proxy_failover.py 用真实的 probe_proxy 输出钉住这层耦合。
    if "代理拒绝 connect" in msg or "连不上代理" in msg:
        return True
    return (
        "connect tunnel failed" in msg
        or "proxy connect" in msg
        or "tunnel connection failed" in msg
        or "curl: (56)" in msg
        or "curl: (97)" in msg
    )


#: 代理明确拒绝 CONNECT 时，哪些状态码足以确认「这条代理现在服务不了我们」。
#: 判据是**换个出口 IP 也没用**：
#:
#: - 402 流量配额耗尽 / 账户欠费、407 认证失败 —— 账户级，整条代理都用不了
#: - 502 代理连不到目标、503 代理服务不可用 —— 代理服务端自身故障
#:
#: 不含 403（该出口被代理商禁用）与 429（代理侧限流）：换个 session 或等一会
#: 就能恢复，据此让整条代理进冷却等于把还能用的容量白白关掉。
_PROXY_SERVICE_DOWN_CODES = frozenset({402, 407, 502, 503})

#: 代理拒绝 CONNECT 时状态码出现的两种形态：
#: libcurl 写 ``CONNECT tunnel failed, response 402``；
#: ``config.probe_proxy()`` 写 ``代理拒绝 CONNECT: 402 Payment Required（…）``。
_PROXY_REJECT_CODE_RE = _re.compile(r"(?:response|connect:)\s*(\d{3})")


def is_proxy_service_error(exc: BaseException) -> bool:
    """
    判断一次代理错误是否足以确认“代理服务端异常”。

    这比 ``is_proxy_error`` 更严格：普通连接抖动、timeout、TLS 中断只算疑似
    代理故障；只有代理服务明确返回 502/Bad Gateway、给出账户级拒绝码，或带上
    provider 自己的错误头/原因（如 Webshare 的 X-Webshare-* / circuit
    breaker），才允许进入 cooldown/fallback。

    2026-08-05 的 402（配额耗尽）此前不在此列——代理连着 5 小时明确回
    ``response 402``，却始终只被当作「疑似」，冷却与降级一次都没触发。
    """
    text = _exception_chain_text(exc)
    if not text:
        return False

    m = _PROXY_REJECT_CODE_RE.search(text)
    if m and int(m.group(1)) in _PROXY_SERVICE_DOWN_CODES:
        return True

    provider_markers = (
        "x-webshare-error",
        "x-webshare-reason",
        "internal_error_auth_circuit_breaker_open",
        "webshare",
    )
    if any(marker in text for marker in provider_markers) and (
        "502" in text or "bad gateway" in text or "circuit_breaker" in text
    ):
        return True

    return (
        ("connect tunnel failed" in text or "tunnel connection failed" in text or "proxy connect" in text)
        and ("502" in text or "bad gateway" in text)
    )


def _exception_chain_text(exc: BaseException) -> str:
    """合并异常链文本，避免 curl_cffi 把代理响应细节藏在 __cause__ 里。"""
    parts: list[str] = []
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        parts.append(type(cur).__name__)
        parts.append(str(cur))
        cur = cur.__cause__ or cur.__context__
    return "\n".join(parts).lower()


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


class OperationNotAllowedError(Exception):
    """
    上游按 GraphQL **operation 白名单**放行，而我们发的这一条不在名单里。

    与 BlockedError 的区别 —— 这是整个类存在的理由
    ------------------------------------------------
    两者都是 HTTP 403，但成因、修法、代价完全相反：

    - BlockedError → Cloudflare 说「不服务**这个 IP**」。正文是挑战页 HTML，
      换出口 IP / 换指纹 / 等冷却都有意义。
    - OperationNotAllowedError → 应用层说「不认识**这条 operation**」。正文是
      JSON（``content-type: application/json``），Cloudflare 只是转发。
      **和 IP 毫无关系**：同一个会话里换一条已登记的 operation 立刻 200。

    把后者当成前者的代价是实打实的。2026-08-19 一次自动预订失败：
    连续两次「重建 CF 会话」各跑一轮完整挑战，75 秒、约 3 MB 代理流量，
    结束时仍是同一个 403；随后误判触发 1 小时登录链路抑制，把本来只坏了
    预订的故障扩散到整条登录路径。换多少个 IP 都不会好。

    唯一的修法是把站点自己发的那条 operation 原样照抄回来
    （见 ``docs/H2S.md`` §5.1）。因此它**不该进任何自动冷却**——等待不会
    改变结果，重试只是重复烧钱。
    """


#: 403 正文里出现这些 = 上游按 operation 白名单拒绝，与出口 IP 无关。
#:
#: 同一道闸门，两种文案，都实测过：
#:   抓取侧 2026-08-18  ``{"code":"operation_not_allowed"}``
#:   预订侧 2026-08-19  ``{"error":"This operation is not available through the public API"}``
#: 只认其中一条会漏判另一条——漏判的后果就是上面那段 75 秒白烧。
_OPERATION_REJECTED_MARKERS: tuple[str, ...] = (
    "operation_not_allowed",
    "not available through the public api",
)


def is_operation_rejected_body(body: str) -> bool:
    """判断 403 响应体是否为「这条 operation 没登记」而非 CF 屏蔽。

    只看正文，不看 ``content-type``：JSON 头是强信号但不是判据——CF 自己的
    某些拒绝也带 JSON 头，而这两句文案是上游应用独有的。
    """
    lower = body.lower()
    return any(marker in lower for marker in _OPERATION_REJECTED_MARKERS)



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

    调用方是 ``browser_fetcher._h2s_maintenance_check``（H2S_PROFILE 的
    ``maintenance_check`` 钩子），在 CF 挑战解开**之后**拿页面正文来判——
    挑战页是 Cloudflare 生成的，解开前的标题/正文与站点真实状态无关。

    曾经还有一个 ``probe_h2s_maintenance(session)``：连续 403 时用 curl_cffi
    另开一次主站 GET 来探维护。H2S 迁到浏览器传输层后它再无调用者（浏览器
    导航本来就会经过主站，顺手判掉即可），已删除。
    """
    if not body:
        return False
    lower = body.lower()
    return any(marker in lower for marker in _MAINTENANCE_MARKERS)


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
    - 可选覆盖 ``batch_session()`` / ``invalidate_session()``（见下）

    **抓取层不管预订。** 这里曾经还挂着 ``prewarm_session()`` 与
    ``try_book(listing)`` 两个 no-op 钩子，设想是「支持自动预订的平台各自实现」。
    实际从未接线：预登录走 ``mcore/prewarm.py → booker.create_prewarmed_session``，
    下单走 ``monitor`` 直接调 ``booker`` / ``bookers/*``，全仓库没有一处调过这
    两个钩子。而 H2S 那个 override 里还留着一句「暂未适配新 API」——它在 booker
    换成 NextAuth（v1.16.9）之后就是错的，等于在基类文档上给后来人指一条不存在
    的路。2026-08-20 删除。

    线程模型
    --------
    实例由 ``get_scraper()`` 按 source 缓存并**跨轮复用**，dispatcher 逐
    source 串行调用，所以同一实例不会被并发进入。

    浏览器型 source（H2S / Xior）还有一条更强的约束：Playwright 对象**绑定
    创建它的线程**，换线程即失效，且两个独立的 Playwright sync 实例不能共存
    于同一线程。因此它们各自恒定跑在 ``monitor._get_browser_executor(source)``
    的专属长存单线程上。往这类 scraper 里塞自己的线程池会直接抛
    ``greenlet.error: Cannot switch to a different thread``。

    纯 HTTP 的 source（OurDomain）没有这个约束，跑在默认 executor 上。
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

    def invalidate_session(self) -> None:
        """丢弃本 source 持有的长生命周期资源（浏览器 / 会话）。

        dispatcher 在三种情况下调用：

        - **未预期异常**（例如 Playwright 的 ``greenlet.error``）——底层会话
          已进入不可用状态，留着只会让后续每一轮都重复失败
        - **本批次出现过 403**——批次跑完后调用（不是抓到就调：批次中间丢会话
          会让同 source 的后续 task 各自触发一次浏览器重建，每次一轮完整 CF
          挑战）。被 CF 标记的会话留着只会一直 403
        - **批次会话本身失败**（``batch_session()`` 的进入或退出抛异常）

        429 和平台维护**不**调用：前者「等等就好」，重建只是白白多过一次挑战；
        后者是对方的事，本地会话没坏。

        默认 no-op：不持有跨 task 资源的 scraper 无需实现。
        """

    @contextmanager
    def batch_session(self):
        """
        批量抓取作用域：dispatcher 在调用本 source 的一组 ``scrape()`` 之前
        进入此上下文，结束后退出。子类可在此创建一个**跨该 source 所有 task
        复用的会话**（一次 TLS 握手 + 一个固定指纹），避免每个城市单独建连。

        为什么需要
        ----------
        P0 把"遍历城市"的循环从 ``scrape()`` 内部提到了 dispatcher，但 Session
        生命周期没跟着提——若 ``scrape()`` 每次自建 Session，N 个城市 = N 次
        TLS 握手 + N 个不同 TLS 指纹（同 IP 快速换指纹对 Cloudflare 是 bot
        信号）。子类重写本方法把 Session 提升到批次级，恢复 P0 之前的行为。

        默认 no-op：子类（如 OurDomain，它本就要 per-task 轮换指纹）不重写
        时，``scrape()`` 各自管自己的会话，行为不变。

        线程模型
        --------
        dispatcher 在单个 executor 线程里顺序处理一个 source 的所有 task，
        所以批次作用域内的共享 Session 无并发风险。
        """
        yield
