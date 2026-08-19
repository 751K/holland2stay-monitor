"""
browser_fetcher.py — 站点无关的浏览器传输层
==============================================

给被 Cloudflare 挑战保护的站点提供统一的请求能力：过挑战 → 等 clearance →
在浏览器内发**同源**请求。浏览器内 fetch() 自动携带 cookies / TLS 指纹 /
CF clearance token，无需手动管理会话。

站点差异全部收在 ``SiteProfile`` 里（challenge_url / 默认头 / clearance 判据
/ 维护检测钩子 / 是否轮换出口 IP），其余流程对所有站点通用。当前有两个
profile：``H2S_PROFILE``（GraphQL）和 ``XIOR_PROFILE``（WordPress admin-ajax）。

使用者：``scrapers/holland2stay.py``、``scrapers/xior.py``、``booker.py``。

线程安全
--------
每个 BrowserFetcher 实例**绑定创建它的线程**——Playwright 对象换线程即失效。
Scraper 侧由 ``monitor._get_browser_executor(source)`` 保证每个浏览器型
source 恒定跑在自己的长存单线程上；Booker 在自己的线程里另建实例。

两个独立的 Playwright sync 实例**不能共存于同一线程**（第一个会在该线程装上
event loop，第二个 launch() 随即被判成「在 asyncio loop 里用同步 API」），
所以 executor 必须按 source 分开，不能共用一条。
"""
from __future__ import annotations

import logging
import platform
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional
from json import dumps as _json_dumps

logger = logging.getLogger(__name__)

# ── 延迟导入异常类 ──────────────────────────────────────────────────
# browser_fetcher 被 scrapers/holland2stay 导入，而 scrapers.base（定义
# 异常的模块）通过 scrapers/__init__.py 提前触发了 holland2stay 的加载。
# 若在模块顶层 import scrapers.base 会导致循环导入。
# 异常只在 fetch_gql 实际遇到错误时才需要，故延迟到首次 raise 时加载。
_exc_cache: dict[str, type] = {}


def _exc(name: str) -> type:
    if name not in _exc_cache:
        from scrapers.base import (  # noqa: E402
            BlockedError,
            OperationNotAllowedError,
            RateLimitError,
            ScrapeNetworkError,
            UpstreamMaintenanceError,
        )
        _exc_cache.update({
            "BlockedError": BlockedError,
            "OperationNotAllowedError": OperationNotAllowedError,
            "RateLimitError": RateLimitError,
            "ScrapeNetworkError": ScrapeNetworkError,
            "UpstreamMaintenanceError": UpstreamMaintenanceError,
        })
    return _exc_cache[name]


#: 从 GraphQL 文档正文里读出 operation 名，例如 ``query GetProduct($k: String!)``
#: → ``GetProduct``。
_OPERATION_LABEL_RE = re.compile(
    r"\b(?:query|mutation|subscription)\s+([A-Za-z_][A-Za-z0-9_]*)"
)


def _operation_label(query: str) -> str:
    """给错误消息用的 operation 名，**不参与请求**。

    调用方没传 ``operation_name`` 时（booker 的 9 条 mutation 都没传）从文档
    正文里读一个出来。纯展示用途：往请求体里补 operationName 会改变发给上游
    的内容，而上游正是按 operation 判放行的——为了让日志好看去动线上行为，
    因果就反了。
    """
    m = _OPERATION_LABEL_RE.search(query or "")
    return m.group(1) if m else "(匿名)"


def _is_operation_rejected(body: str) -> bool:
    """403 正文是否表示「这条 operation 没在上游白名单里」。

    判据本身住在 ``scrapers.base``（和 ``is_cloudflare_body`` 这些放一起，
    抓取侧与预订侧共用一份文案清单）；这里只是同样的延迟导入包装，理由见
    上面 ``_exc`` 的注释——模块顶层 import 会循环。
    """
    from scrapers.base import is_operation_rejected_body  # noqa: E402

    return is_operation_rejected_body(body)


_H2S_MAIN_PAGE = "https://www.holland2stay.com/residences"
# GraphQL 端点。H2S 迁过两次：
#   api.holland2stay.com/graphql  →  www.holland2stay.com/api/graphql
#                                 →  www.holland2stay.com/api/service/residences
#
# 2026-08-11 19:34 第二次迁移，旧路径直接 404（Next.js 错误页，不是 JSON）。
# 404 不在 clearance_pending_markers 里，于是被当成硬网络错误——整个 source
# 每轮隔离，连带把「所有城市第 1 页都连不上」判成全网失败。生产静默了三天。
#
# 同一次改版还上了一层加密信道：客户端 AES-GCM 加密请求体、RSA-OAEP 包裹会话
# 密钥，POST 成 {v,k,iv,d,ct} 信封。但它只作用于 axios 拦截器命中的
# `/api/rest/*`（源码里的判据就是 `url.startsWith("/api/rest/")`）；
# `/api/service/residences` 不在其中，仍是明文 GraphQL——所以这里不需要复刻
# 那套加密。它哪天挪进 /api/rest/ 才需要，那时得从页面 webpack runtime 里取
# 出加密函数来用，而不是自己实现。详见 docs/H2S.md。
#
# **改这个常量前先确认新路径返回的是 GraphQL 错误而不是 HTML**：打一个空 body，
# 端点对了会回 `{"errors":[{"message":"Syntax Error: Unexpected <EOF>"...}]}`。
#
# 2026-08-17 第三次迁移：/api/service/residences 也 404 了，GraphQL 整体搬进
# 加密信道 /api/__enc__。**schema 一字未改**——截获站点加密前的明文可见，它发的
# 仍是 GetCategories / products / category_uid:"Nw==" / 同一套 available_to_book
# ID。变的只有传输层，所以 _GQL_QUERY 与 _to_listing 都不用动。见 docs/H2S.md §4。
_H2S_GQL_PATH = "/api/__enc__"

# 信封请求头。值恒为 "1"：请求上表示 body 是信封，响应上表示 body 需要解密。
_ENC_HEADER = "x-enc"
# ── /api/rest/* 的信封约定（2026-08-19 逐字读自站点 module 82361）──────
# 站点对 REST 的加密**和 GraphQL 不同**，分 GET / 非 GET 两套：
#
#   非 GET（函数 J）：加密 **body** → POST 原 URL，头 x-enc: 1
#                    ——形状与 GraphQL 一致，可直接复用 _encrypted_fetch
#   GET（函数 H）：  加密 **路径本身**（去掉 /api 前缀，含 query string），
#                    base64(JSON(信封)) 塞进 x-enc-q 头，
#                    实际请求 GET /api/rest/__enc__
#
# 之前 cancel_pending_orders 直接发明文 /api/rest/...，两条都不对。
_ENC_QUERY_HEADER = "x-enc-q"
_REST_ENC_PATH = "/api/rest/__enc__"
_REST_API_PREFIX = "/api"

# 公钥在 bundle 里是个 SPKI base64 常量（实测 392 字符）。运行时抓而不是写死：
# 它可能轮换，写死会在轮换当天变成一次无从下手的解密失败。
_ENC_PUBKEY_RE = r'"(MII[A-Za-z0-9+/=]{80,})"'

# 只在这些 chunk 里找公钥。**别再扫全部 script。**
#
# 2026-08-18 生产事故：初版实现遍历页面上全部 <script src>（实测 81 个）逐个
# fetch，直到命中含 __enc__ 的那个。代价是每建一次浏览器多打 ~97 个请求：
#
#     Holland2Stay   217 → 314 个响应 / 会话，1.78 → 2.66 MB
#     Xior（对照组）   93 →  97 个响应 / 会话，0.60 → 0.61 MB
#
# 部署当晚 H2S 就开始连续 403（此前三天一次都没有），熔断退避到 110 分钟。
# 「从页面里把每个 JS chunk 都拉一遍」本身就是极像爬虫的行为特征。
#
# 实测 __enc__ 只出现在 common-* 与 vendors-* 两类 chunk 里，按名字先筛一遍
# 就够，命中即停——正常情况下只需 1–2 个请求。
_ENC_CHUNK_HINTS = ("common-", "vendors-")

# 公钥是站点级常量，**进程内共享**，不随浏览器重建作废。
#
# 初版绑在浏览器生命周期上（2 小时一换），本意是让公钥轮换最多废掉一个会话；
# 但那意味着每次重建浏览器都要重新扫一遍 bundle，而 403 恰恰会触发重建——
# 于是被封之后反而扫得更凶，正反馈。改为进程级缓存 + 失败时作废：轮换照样
# 能自愈（解密失败会清缓存重取），代价却降到每进程一次。
_ENC_PUBKEY_CACHE: dict[str, str] = {}
_XIOR_MAIN_PAGE = "https://www.xiorstudenthousing.eu/netherlands/"
_XIOR_AJAX_PATH = "/wp-admin/admin-ajax.php"

# CF 挑战页判据。几个看起来能用但实测不能用的候选：
#   - ``challenges.cloudflare.com`` / ``/cdn-cgi/challenge-platform/``：
#     挑战解开后的真实页面里同样存在（CSP 头 + 站点自带 turnstile），
#     用它们判定会永远认为挑战没过。
#   - URL 里的 ``__cf_chl_rt_tk``：CF 靠 ``history.replaceState`` 回写 URL，
#     时机不定；实测挑战已解开、真实 DOM 已就位时 URL 仍可能带着它，
#     用它判定会把正常会话误判成被挡。
# 只有挑战页脚本自身的 ``_cf_chl_opt`` 会随文档被真实页面替换而消失。
_CF_CHALLENGE_HTML_MARKER = "_cf_chl_opt"

# 响应体里出现这些 = 拿到的是 CF 挑战页而不是真实响应。用于把「clearance 还
# 没生效」和「这个 IP 被封了」区分开——前者重新导航就好，后者得换 IP。
_CF_CHALLENGE_PENDING_MARKERS: tuple[str, ...] = (
    _CF_CHALLENGE_HTML_MARKER,
    "just a moment",
    "cdn-cgi/challenge-platform",
)

# 挑战解开的等待上限。实测差异很大：macOS 本地约 3s，1 CPU 的生产 VPS 上
# headless Chromium 跑完 challenge 要 30s 量级。上限按最慢的环境留足余量，
# 超时说明这个 IP 当前过不去，交给上层熔断而不是硬发请求。
# Chromium 在代理层失败时给的错误码。真实原因（配额耗尽、认证失败、代理宕机）
# 只有代理自己知道，到了 Playwright 这层一律被压成这几个码，所以命中时必须
# 另行探测——见 ``_describe_navigation_failure``。
_PROXY_ERROR_MARKERS: tuple[str, ...] = (
    "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_PROXY_AUTH_UNSUPPORTED",
    "ERR_NO_SUPPORTED_PROXIES",
    "ERR_PROXY_CERTIFICATE_INVALID",
)

_CHALLENGE_CLEAR_TIMEOUT = 90.0
_CHALLENGE_POLL_INTERVAL = 1.0

# clearance 未生效时 H2S 返回的标记（403 + 这段 JSON）
_CLEARANCE_REQUIRED_MARKER = "clearance_required"
# 探测用的最小查询：只取 total_count，不翻页不取字段
# 探测必须用白名单登记的那条 operation。**不能自己写个最小查询**——
# H2S 自 2026-08-18 起按 operation 白名单放行，匿名/自定义查询一律
# 403 operation_not_allowed，而那不是 clearance_pending_markers 里的标记，
# 于是初始化会把「查询没登记」误判成「这个 IP 被封」，一路熔断。
# 代价只是探测请求变大，一次初始化一次，可接受。
from h2s_gql import GQL_QUERY as _H2S_GQL_DOCUMENT, OPERATION_NAME as _H2S_OPERATION

_CLEARANCE_PROBE_VARIABLES = {
    "pageSize": 1,
    "currentPage": 1,
    "filters": {"category_uid": {"eq": "Nw=="}},
    "sort": {"next_contract_startdate": "ASC"},
}
# 单次导航后等 cookie 落地的上限。实测正常 2–3s（本地）到 10–22s（生产 VPS）。
# 不宜再长：token 只能靠重新导航签发，超过这个窗口还没落地，继续轮询是白打
# 必然 403 的请求，换一次导航才有意义。
_CLEARANCE_TIMEOUT = 25.0
_CLEARANCE_POLL_INTERVAL = 2.0

# 初始化最多导航几次。每次都是一轮完整的 CF 挑战，次数过多反而加重怀疑。
_INIT_ATTEMPTS = 3
_H2S_GQL_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    # Match Magento/Hyva-style storefront GraphQL requests more closely than a
    # bare browser fetch. These are not secrets and are safe for anonymous reads.
    "Store": "default",
    "Content-Currency": "EUR",
}
_XIOR_AJAX_HEADERS = {
    "Accept": "*/*",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
}


# ── 资源拦截 ────────────────────────────────────────────────────────
# 代理按流量计费，而浏览器为了过挑战会把整张页面连同图片、字体、统计脚本
# 一并下载。2026-08-04 全天代理侧记录：985MB 中约 97MB 属于此类，与房源数据
# 无关。我们要的只是 DOM 与 cf_clearance cookie。
#
# 按类型拦。stylesheet 不在其中——CF 的行为检测会读渲染结果，去掉样式表等于
# 改变页面的呈现方式，省下的量（合计不足 5MB）不值这个风险。
_BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font"})

# 按域名整体拦：统计、广告、客服挂件、地理编码。都是页面自己加载的第三方，
# 与「页面是否已渲染完成」无关。
#
# 刻意不含 cdn.jsdelivr.net（4.5MB/天）：站点的业务 JS 也走它，无法从域名
# 区分，拦错会连页面本身一起弄坏。
_BLOCKED_DOMAINS: tuple[str, ...] = (
    "googletagmanager.com",
    "google-analytics.com",
    "googlesyndication.com",
    "fonts.gstatic.com",
    "fonts.googleapis.com",
    "fontawesome.com",
    # cookieyes 的 CDN 挂在另一个注册域上（cdn-cookieyes.com，不是
    # cookieyes.com 的子域），按点边界匹配吃不到，必须单列。它还是这家
    # 三个域里最大的那个。
    "cookieyes.com",
    "cdn-cookieyes.com",
    "trustpilot.com",
    "chatbase.co",
    "ahrefs.com",
    "komoot.io",
)

# 无论类型与域名，这些一律放行——挑战本身要靠它们跑完。
# cloudflareinsights 是 CF 自家的 beacon，量很小（0.4MB/天），拦它省不下什么，
# 却可能让这个会话在 CF 眼里显得不完整。
_NEVER_BLOCKED: tuple[str, ...] = (
    "challenges.cloudflare.com",
    "cloudflareinsights.com",
)


def _should_block(url: str, resource_type: str) -> bool:
    """这个子请求该不该拦。纯函数，便于单测覆盖各种 URL 形态。"""
    from urllib.parse import urlparse

    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False  # 解析不了就放行，拦错的代价高于多下一次
    if not host:
        return False
    if any(host == d or host.endswith("." + d) for d in _NEVER_BLOCKED):
        return False
    if any(host == d or host.endswith("." + d) for d in _BLOCKED_DOMAINS):
        return True
    return resource_type in _BLOCKED_RESOURCE_TYPES


# ── 持久化 profile ──────────────────────────────────────────────────
# 挑战载荷占了代理流量的一多半（2026-08-04：985MB 中 558MB），成因是每次重建
# 浏览器都是全新的空缓存。而 launch() + new_page() 走的是 incognito context，
# HTTP 缓存只在内存里，浏览器一关即弃——只给 --disk-cache-dir 无济于事，实测
# 缓存目录里只会留下几个索引文件，字节数一点不降。
#
# 必须换成 launch_persistent_context()。本地实测（2026-08-05，H2S 首页）：
#   冷 profile  3.93 MB
#   暖 profile  0.25 MB   143 个请求命中磁盘缓存，页面照常渲染
#
# 一个 profile 目录同一时刻只能被一个 Chromium 打开，而 H2S 的 scraper 与
# booker 用的是同一个 profile、跑在不同线程上。因此按槽位加文件锁，抢不到就
# 退回临时 profile——省流量不能以抢不到浏览器为代价。
_PROFILE_SLOTS = 3

# 每个槽位的磁盘缓存上限。生产 VPS 磁盘已用 82%，不能让它无限长。
# 128MB 足够装下挑战载荷与站点静态资源（实测暖 profile 全目录约 12MB）。
_DISK_CACHE_SIZE = 128 * 1024 * 1024


def _profile_root():
    from config import DATA_DIR

    return DATA_DIR / "browser_profiles"


def _acquire_profile_slot(source: str):
    """占一个 profile 槽位，返回 ``(目录, 锁文件对象)``；全被占用时返回 ``(None, None)``。

    锁必须持有到浏览器关闭为止，所以把文件对象一并交出去——提前关闭文件会
    释放 flock，另一个线程就能打开同一个 profile，Chromium 随即报锁冲突。
    """
    import fcntl

    root = _profile_root()
    for slot in range(_PROFILE_SLOTS):
        path = root / f"{source}-{slot}"
        try:
            path.mkdir(parents=True, exist_ok=True)
            handle = open(root / f"{source}-{slot}.lock", "w")
        except OSError as e:
            logger.warning("profile 槽位 %s 不可用: %s", path, e)
            continue
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()  # 已被别的线程/进程占着，试下一个
            continue
        return path, handle
    return None, None


# Chromium 用这三个文件做单实例互斥，内容是指向 ``<hostname>-<pid>`` 的符号链接。
_SINGLETON_FILES = ("SingletonLock", "SingletonSocket", "SingletonCookie")


def _clear_stale_singleton_locks(path) -> None:
    """清掉 profile 里残留的 Chromium 单实例锁。

    容器 force-recreate 时旧 Chromium 是被杀掉的，这三个锁留在了 bind mount
    里。新容器 hostname 变了、锁里记的 pid 也不存在，Chromium 判定该 profile
    正被别的实例占用，启动即退出，Playwright 报
    ``Target page, context or browser has been closed``。

    也就是说：**每次部署都会让持久化 profile 失效**，而且是静默降级——日志里
    只有一行「退回临时 profile」，流量悄悄涨回去。2026-08-05 上线当天就复现了。

    删掉是安全的：同一时刻只有一个实例在用这个目录，这一点已由我们自己的槽位
    flock 保证，Chromium 这层锁对我们是多余的。
    """
    for name in _SINGLETON_FILES:
        target = path / name
        try:
            # 用 lstat 而非 exists()：这些是符号链接，指向的路径通常已经不在了，
            # exists() 会跟随链接并返回 False，于是一个都删不掉。
            target.lstat()
        except OSError:
            continue
        try:
            target.unlink()
        except OSError as e:
            logger.warning("清理 %s 失败，持久化 profile 可能启动不了: %s", target, e)


def _release_lock(handle) -> None:
    """放掉槽位锁。关闭文件即释放 flock，出错也不能往外抛——它挂在 close()
    路径上，抛出去会把浏览器的资源释放一起带停。"""
    if handle is None:
        return
    try:
        handle.close()
    except OSError:
        logger.debug("释放 profile 锁失败", exc_info=True)


def _redact_proxy(url: str) -> str:
    """代理 URL 里含用户名密码，日志里只留 host:port。"""
    return url.rsplit("@", 1)[-1] if "@" in url else url


def _h2s_maintenance_check(title: str, html: str) -> None:
    """H2S 专有：识别平台维护页，命中则抛 UpstreamMaintenanceError。"""
    if "maintenance" in title.lower():
        raise _exc("UpstreamMaintenanceError")(
            f"H2S 平台维护中（页面标题: {title}）"
        )
    if not html:
        return
    from scrapers.base import is_maintenance_body  # 延迟导入，避免循环导入

    if is_maintenance_body(html):
        raise _exc("UpstreamMaintenanceError")("H2S 平台维护中")


@dataclass(frozen=True)
class ProbeRequest:
    """初始化阶段用来确认 clearance 已生效的最小请求。"""

    path: str
    method: str = "POST"
    body: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)


def _gql_body(query: str, variables: dict | None, operation_name: str = "") -> str:
    """拼 GraphQL 请求体。

    ``operationName`` 只在非空时才写进去——H2S 自 2026-08-18 起按 operation
    白名单放行（见 docs/H2S.md §5），而 Xior 那边根本不发 GraphQL。多塞一个
    ``"operationName": ""`` 会改变请求体，没必要冒这个险。
    """
    body: dict = {"query": query, "variables": variables or {}}
    if operation_name:
        body["operationName"] = operation_name
    return _json_dumps(body)


@dataclass(frozen=True)
class SiteProfile:
    """一个受 Cloudflare 保护的站点，需要浏览器传输层才能访问。

    把「过挑战 → 等 clearance → 发同源请求」这套流程里所有**站点相关**的
    部分收进来，其余逻辑（挑战判据、重试、超时）对所有站点通用。

    字段
    ----
    name            日志用的站点名
    source          对应的 source 名，用于取该 source 专属的代理 session
    challenge_url   过 CF 挑战时导航到的页面。它同时决定了同源请求的 origin，
                    所以必须和后续请求同域。
    default_headers 该站点请求的默认头
    clearance_probe 可选。有的话，初始化最后一步会用它确认 clearance 真的生效
                    （见 ``_wait_for_clearance``）。没有就跳过，交给首个真实
                    请求遇到 403 时重新导航。
    clearance_pending_markers
                    403 响应里出现这些标记 = clearance 还没生效（**瞬时**，
                    重新导航即可），而不是这个 IP 被封（换 IP 才有用）。
    maintenance_check
                    可选钩子，收到 (title, html) —— 仅在挑战解开后调用。
    rotating_proxy  True 时每次创建浏览器都换一个代理 session（即换出口 IP）。

                    **换 IP 的时机是「建浏览器」，不是「每请求」**，所以它
                    并不牺牲 clearance 复用：clearance 本来就只在单个浏览器
                    的生命周期内有效，而**新建浏览器无论如何都要重解一次挑战**
                    ——那一刻用旧 IP 还是新 IP，成本完全一样。

                    反过来，固定 IP 在这一刻省不下任何东西，却带来一个真实
                    代价：**IP 一旦被 CF 盯上就再也换不掉**（session id 是
                    source 名的哈希，恒定），403 之后 invalidate_session()
                    重建出来的还是同一个 IP，恢复路径形同虚设。

                    结论：除非有理由要长期钉住某个出口 IP，否则都该开。
    """

    name: str
    source: str
    challenge_url: str
    default_headers: Mapping[str, str] = field(default_factory=dict)
    clearance_probe: Optional[ProbeRequest] = None
    clearance_pending_markers: tuple[str, ...] = _CF_CHALLENGE_PENDING_MARKERS
    maintenance_check: Optional[Callable[[str, str], None]] = None
    rotating_proxy: bool = False
    #: 请求体是否要包成加密信封。见 ``BrowserFetcher._encrypted_fetch``。
    #: 只影响传输层：调用方照旧传明文 body、拿到明文 text。
    encrypted_envelope: bool = False


H2S_PROFILE = SiteProfile(
    name="Holland2Stay",
    source="holland2stay",
    challenge_url=_H2S_MAIN_PAGE,
    default_headers=_H2S_GQL_HEADERS,
    clearance_probe=ProbeRequest(
        path=_H2S_GQL_PATH,
        body=_gql_body(
            _H2S_GQL_DOCUMENT, _CLEARANCE_PROBE_VARIABLES, _H2S_OPERATION,
        ),
        headers=_H2S_GQL_HEADERS,
    ),
    # H2S 不回 CF 挑战页，而是自己的 JSON：
    #   403 {"error":"Browser verification required","code":"clearance_required"}
    clearance_pending_markers=(_CLEARANCE_REQUIRED_MARKER,)
    + _CF_CHALLENGE_PENDING_MARKERS,
    maintenance_check=_h2s_maintenance_check,
    # GraphQL 自 2026-08-17 起只走加密信道，明文直接吃 Cloudflare 挑战。
    encrypted_envelope=True,
    # 2026-08-03 生产事故：出口 IP 被 CF 盯上后，H2S 连续 3 次 90s 挑战全失败，
    # 熔断退避 30 分钟。而 sticky session id 是 sha1(source) 的常量——重建浏览器
    # 拿到的还是同一个 IP，403 恢复路径根本走不出去。
    #
    # 开 rotating 不损失 clearance 复用：浏览器存活 2 小时（_BROWSER_MAX_AGE），
    # 这期间 IP 和 clearance 都稳定；而重建浏览器本来就要重解挑战，那一刻换个
    # 新 IP 是免费的。
    rotating_proxy=True,
)

# Xior 的 AJAX 端点要 property/room id 才能拿到有意义的响应，profile 层拿不到，
# 所以不配 probe：初始化只等挑战解开，clearance 没生效时由首个真实请求触发
# 重新导航（fetch() 内处理）。
XIOR_PROFILE = SiteProfile(
    name="Xior",
    source="xior",
    challenge_url=_XIOR_MAIN_PAGE,
    default_headers=_XIOR_AJAX_HEADERS,
    # Xior 的 AJAX 端点按 IP 累积限流（~15–20 req/window）。固定出口 IP 上
    # 每轮 12 个请求，几轮之后必然 429——2026-08-02 实测，第 2 轮第一个请求
    # 就被拒。换浏览器就换 IP，把累积量摊开。
    rotating_proxy=True,
)


class BrowserFetcher:
    """
    管理 CloakBrowser 生命周期，在浏览器内发**同源**请求。

    浏览器内 fetch() 自带 cookies / TLS 指纹 / CF clearance token。

    **浏览器是过挑战所必需的，但发请求并不必需。** 此处原先写着「脱离浏览器把
    cookie 搬给 HTTP 客户端通常无效，因为 clearance 同时绑定了 TLS 指纹」——
    2026-08-05 实测该结论不成立：

        浏览器过一次挑战 → 导出 cf_clearance 与 UA
        curl_cffi 带上二者 + **同一个出口 IP** + Chrome 指纹
        → GraphQL 返回 200

        chrome131 / chrome136 / chrome124 三种指纹均可

    clearance 确实绑 IP，但 curl_cffi 的 Chrome 指纹伪装已足够接近，不再是障碍。

    **但据此改造省不了流量，反而更费。** 同日用 ``tools/clearance_probe.py`` 实测
    了一张票离开浏览器之后能活多久：

        0 / 5 / 10 / 15 分钟   200
        20 / 25 / 30 分钟      403 «Just a moment...»
        → 真实寿命 15–20 分钟

    ``cf_clearance`` 上标称的一年是摆设；真正管事的是同域下的 ``h2s_clr``，
    它的过期时间正好是 **0.5 小时**。

    而浏览器内的会话能撑 2 小时（``_BROWSER_MAX_AGE``），因为它一直在正常发请求、
    cookie 由服务端持续刷新；票一旦离开浏览器就没人续了。也就是说改用 curl_cffi
    之后**过挑战的频率只会上升**——从 2 小时一次变成 15–20 分钟一次。

    挑战载荷（2026-08-04：985MB 中 558MB）只能靠**减少浏览器重建次数**来省，也就是
    持久化 profile（已做）与放宽重建周期。这条路走不通。

    换出口 IP 必须重新过挑战，这一点也不会改变。

    站点差异全部收在 ``SiteProfile`` 里（见该类）；本类只负责通用流程：
    过挑战 → 等 clearance → 发请求 → 处理 clearance 过期。

    用法
    ----
    ::

        with BrowserFetcher(profile=H2S_PROFILE) as f:
            data = f.fetch_gql(query, variables)

        with BrowserFetcher(profile=XIOR_PROFILE) as f:
            resp = f.fetch_form("/wp-admin/admin-ajax.php", {"action": "..."})

    资源
    ----
    空闲 ~190MB，3 个 tab ~280MB。使用完后必须 close() 或通过上下文管理器释放。
    """

    def __init__(
        self,
        headless: bool = True,
        profile: "SiteProfile" = None,  # type: ignore[assignment]
    ):
        # 默认 H2S：booker 和 H2S scraper 都按位置参数调用，保持向后兼容
        self._profile = profile if profile is not None else H2S_PROFILE
        self._headless = headless
        self._browser = None
        self._page = None
        self._initialized = False
        self._effective_headless = headless
        self._proxy_url = ""
        self._blocked_count = 0
        self._profile_lock = None
        self._profile_path = None
        # 见 _install_byte_accounting
        self._wire_bytes = 0
        self._response_count = 0
        self._cached_count = 0
        self._cdp = None

    @property
    def profile(self) -> "SiteProfile":
        return self._profile

    # ── 上下文管理器 ──────────────────────────────────────────────────
    def __enter__(self) -> "BrowserFetcher":
        self._launch()
        return self

    def _launch(self) -> None:
        from cloakbrowser import launch

        if platform.system() == "Darwin" and self._effective_headless:
            logger.info("macOS 本地 CloakBrowser 使用 headed 调试模式，避免 headless SIGABRT")
            self._effective_headless = False

        # Docker/Linux 兼容参数：
        # - disable-dev-shm-usage: /dev/shm 默认 64MB，Chromium 会崩，改用 /tmp
        # - disable-gpu: headless 不需要 GPU 加速，避免无 GPU 环境报错
        #
        # macOS 本地 CloakBrowser headless v145 存在 SIGABRT 风险；这些 Linux
        # 参数也不该无条件注入到本地调试浏览器。
        chromium_args = []
        if platform.system() == "Linux":
            chromium_args = ["--disable-dev-shm-usage", "--disable-gpu"]

        # 代理必须**显式**传给 launch()，不能指望 Chromium 自己解析
        # HTTP_PROXY / HTTPS_PROXY 环境变量。
        #
        # 实测（2026-08-02，webshare sticky residential 端点）：
        #   仅环境变量   → 浏览器 79.116.229.115 / curl_cffi 213.73.166.139
        #   显式 proxy=  → 两者都是 213.10.194.180（连续两轮复现）
        # 环境变量那条路拿到的出口甚至不在 sticky 端点指定的国家，说明
        # Chromium 走的是另一套代理解析。
        #
        # 后果不止是「不一致」：Cloudflare 的 clearance 绑 IP，浏览器与
        # 其它 curl_cffi 抓取器落在不同出口，就无法共享会话；而且浏览器
        # 会完全绕过 get_proxy_url() 的故障切换与冷却逻辑。
        #
        # 配置来源仍然只有 .env 一处——get_proxy_url() 读的就是那几个环境
        # 变量，这里只是把它已经解析好的值显式交给浏览器。
        from config import get_proxy_url  # 延迟导入，避免循环依赖

        proxy_url = get_proxy_url(
            self._profile.source, rotating=self._profile.rotating_proxy
        ) or None
        if proxy_url:
            logger.info(
                "%s 浏览器走代理 %s",
                self._profile.name, _redact_proxy(proxy_url),
            )
        # 记下来给 _describe_navigation_failure 用：诊断必须探**这个浏览器实际
        # 在用**的那条代理线路。重新调 get_proxy_url() 在 rotating 的 profile 上
        # 会拿到另一个 session，探到的是别的出口 IP，结论无效。
        self._proxy_url = proxy_url or ""

        self._browser, self._page = self._open_browser(chromium_args, proxy_url)
        self._blocked_count = 0
        self._wire_bytes = 0
        self._response_count = 0
        self._cached_count = 0
        self._cdp = None
        self._install_resource_blocking()
        self._install_byte_accounting()

    def _open_browser(self, chromium_args: list[str], proxy_url: str | None):
        """开浏览器，优先复用磁盘上的 profile；拿不到就退回临时 profile。

        返回 ``(browser_or_context, page)``。两种返回物的 ``close()`` 与
        ``new_page()`` 语义一致，本类其余部分不必区分。
        """
        import os

        from cloakbrowser import launch, launch_persistent_context

        if os.environ.get("BROWSER_PERSIST_PROFILE", "1").strip() not in ("0", "false", "no"):
            path, lock = _acquire_profile_slot(self._profile.source)
            if path is None:
                logger.info(
                    "%s 的 profile 槽位已被占满（共 %d 个），本次用临时 profile",
                    self._profile.source, _PROFILE_SLOTS,
                )
            else:
                # 必须在 launch 之前：Chromium 一见到别人的锁就直接退出，
                # 事后再清也救不回这一次。
                _clear_stale_singleton_locks(path)
                try:
                    ctx = launch_persistent_context(
                        str(path),
                        headless=self._effective_headless,
                        humanize=True,
                        args=chromium_args + [f"--disk-cache-size={_DISK_CACHE_SIZE}"],
                        proxy=proxy_url,
                    )
                except Exception as e:
                    # profile 损坏、锁冲突、磁盘满……都不该让抓取停摆
                    logger.warning(
                        "%s 持久化 profile 启动失败，退回临时 profile: %s",
                        self._profile.name, e,
                    )
                    _release_lock(lock)
                else:
                    self._profile_lock = lock
                    self._profile_path = path
                    # cookie 一律不留。clearance 绑出口 IP，而 rotating_proxy
                    # 意味着下次开浏览器多半换了 IP——带着上一个 IP 的
                    # cf_clearance 去请求，CF 只会当作可疑并重新挑战。
                    # 要复用的只是磁盘缓存里那些静态资源。
                    try:
                        ctx.clear_cookies()
                    except Exception:
                        logger.debug("清 cookie 失败，继续", exc_info=True)
                    page = ctx.pages[0] if ctx.pages else ctx.new_page()
                    logger.info("%s 复用 profile %s", self._profile.name, path.name)
                    return ctx, page

        browser = launch(
            headless=self._effective_headless,
            humanize=True,
            args=chromium_args,
            proxy=proxy_url,
        )
        return browser, browser.new_page()

    def _install_resource_blocking(self) -> None:
        """拦掉与房源数据无关的子请求，省代理流量。见 ``_should_block``。

        ``BROWSER_BLOCK_RESOURCES=0`` 可整体关闭：拦截会改变页面的加载行为，
        万一 CF 因此起疑，需要一条不重新发版就能退回原状的路。
        """
        import os

        if os.environ.get("BROWSER_BLOCK_RESOURCES", "1").strip() in ("0", "false", "no"):
            logger.info("%s 资源拦截已关闭（BROWSER_BLOCK_RESOURCES）", self._profile.name)
            return

        def _handler(route):
            # 任何一步出错都必须放行并吞掉异常：route 处理器抛出去会让这个
            # 请求悬着直到超时，为省流量把页面拖垮不划算。
            try:
                request = route.request
                if _should_block(request.url, request.resource_type):
                    self._blocked_count += 1
                    route.abort()
                    return
            except Exception:
                logger.debug("资源拦截判定异常，放行", exc_info=True)
            try:
                route.continue_()
            except Exception:
                logger.debug("route.continue_ 失败", exc_info=True)

        try:
            self._page.route("**/*", _handler)
        except Exception as e:
            # 拦截是省钱的优化，不是抓取的前提，装不上就照常跑
            logger.warning("%s 资源拦截未能装载，按原样加载页面: %s", self._profile.name, e)

    def _install_byte_accounting(self) -> None:
        """统计本次会话真实走了多少字节。``BROWSER_BYTE_ACCOUNTING=0`` 关闭。

        为什么必须是 CDP 而不是别的
        --------------------------
        代理按流量计费，而在这之前**没有任何地方知道浏览器到底下了多少字节**：

        - ``route.abort()`` 在下载之前就掐断了，被拦掉的请求根本没有大小；
        - ``response.body()`` 要把整个 body 拉进内存，为了称重去下载它，
          省流量的事就白做了；
        - ``Content-Length`` 在 chunked 响应上直接缺席。

        ``Network.loadingFinished.encodedDataLength`` 是 Chromium 自己记的
        **线上字节数**，含响应头、按压缩后计算，不额外产生任何请求。

        顺带记 ``fromDiskCache``：持久化 profile 声称复用磁盘缓存来省流量，
        但每次会话都换出口 IP 又清了 cookie，缓存到底命中没有一直没人验证过。
        命中的响应 encodedDataLength 记 0，两个数放一起就能看出来。

        全程 try/except：这是计量，不是抓取的前提，任何一步失败都只降级成
        「这次没数」，不能影响页面加载。
        """
        import os

        if os.environ.get("BROWSER_BYTE_ACCOUNTING", "1").strip() in ("0", "false", "no"):
            return

        try:
            cdp = self._page.context.new_cdp_session(self._page)
        except Exception as e:
            # 非 Chromium 内核、或 CDP 不可用 → 没有计量，照常抓
            logger.debug("%s 字节计量未能装载: %s", self._profile.name, e)
            return

        def _finished(params):
            try:
                self._wire_bytes += int(params.get("encodedDataLength") or 0)
            except Exception:
                pass

        def _response(params):
            try:
                self._response_count += 1
                if (params.get("response") or {}).get("fromDiskCache"):
                    self._cached_count += 1
            except Exception:
                pass

        try:
            cdp.send("Network.enable")
            cdp.on("Network.loadingFinished", _finished)
            cdp.on("Network.responseReceived", _response)
        except Exception as e:
            logger.debug("%s 字节计量启用失败: %s", self._profile.name, e)
            return
        self._cdp = cdp

    def _rebuild_browser(self) -> bool:
        """推倒浏览器重建，换一个出口 IP。换成了返回 True。

        403 和挑战超时都是「当前这个出口 IP 过不去」。同一个浏览器再导航一次
        用的还是同一个 IP，重试等于把同一次失败重复三遍——2026-08-03 生产事故
        就是这么熔断的。

        只有 ``rotating_proxy`` 的 profile 重建才会落到新 session；固定 IP 的
        profile 重建后拿到的是同一个出口，白付一次冷启动，所以不做。
        """
        if not self._profile.rotating_proxy:
            return False
        if self._browser is None:
            # 还没 __enter__ 就走到这里，说明调用方绕过了正常生命周期。
            # 凭空 launch 一个浏览器不是「重建」，交回给调用方处理。
            return False
        old = _redact_proxy(self._proxy_url) if self._proxy_url else "直连"
        self.close()
        self._launch()
        new = _redact_proxy(self._proxy_url) if self._proxy_url else "直连"
        logger.info(
            "%s 重建浏览器以更换出口 IP：%s → %s", self._profile.name, old, new
        )
        return True

    def __exit__(self, *args):
        self.close()

    def close(self):
        """关闭浏览器，释放资源。"""
        if self._browser is not None:
            if self._blocked_count or self._wire_bytes:
                # 拦了多少、下了多少写进日志：改了拦截规则之后，这是唯一能看出
                # 它还在生效、以及生效到什么程度的地方。两个数必须在同一行——
                # 拦截数单独看只能说明「拦到了」，说明不了省下多少钱。
                logger.info(
                    "%s 本次会话拦截 %d 个子请求，实际下行 %.2f MB（%d 个响应，%d 个命中磁盘缓存）",
                    self._profile.name, self._blocked_count,
                    self._wire_bytes / 1e6, self._response_count, self._cached_count,
                )
            try:
                if self._cdp is not None:
                    self._cdp.detach()
            except Exception:
                pass
            self._cdp = None
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
            self._page = None
            self._initialized = False
        # 锁必须在浏览器真正关掉之后才放：提前释放会让另一个线程拿到同一个
        # profile，而 Chromium 还没退出，它会直接报锁冲突。
        _release_lock(self._profile_lock)
        self._profile_lock = None
        self._profile_path = None

    # ── CF 挑战初始化 ─────────────────────────────────────────────────
    def ensure_initialized(self) -> None:
        """
        懒加载：首次请求前访问主页完成 CF Turnstile 挑战。

        公开方法——booker 可以提前调用来热身，也可以在 fetch_gql 首次调用时自动触发。
        """
        if self._initialized:
            return

        start = time.monotonic()
        last_error: Exception | None = None

        # 每次尝试 = 一次完整导航 + 短暂等 cookie 落地。等不到就**重新导航**，
        # 而不是继续对 API 轮询：token 由导航签发，轮询换不出来，只会朝一个
        # 拿不到 clearance 的会话打几十个必然 403 的请求，反过来加重 CF 的
        # 怀疑。（同理见 fetch_gql 里 clearance 过期的处理。）
        for attempt in range(1, _INIT_ATTEMPTS + 1):
            try:
                challenge_elapsed, clearance_elapsed = self._navigate_and_verify(
                    attempt
                )
            except _exc("BlockedError") as e:
                last_error = e
                logger.warning(
                    "初始化第 %d/%d 次未通过：%s", attempt, _INIT_ATTEMPTS, e
                )
                # 挑战没解开或 clearance 不生效，说明是这个出口 IP 被盯上了。
                # 重建浏览器换个 IP 再试，同一个 IP 上重来三次没有意义。
                if attempt < _INIT_ATTEMPTS:
                    self._rebuild_browser()
                continue

            # 两段耗时按**成功的那次导航**分开记（不含前面失败尝试的耗时，
            # 否则失败的等待会被算进 clearance）：挑战慢通常是机器/网络慢，
            # clearance 慢更像 CF 在加码校验，排查时是两个方向。
            logger.info(
                "CF 挑战完成，clearance 已生效 "
                "(第 %d 次导航：挑战 %.1fs + clearance %.1fs；累计 %.1fs)",
                attempt, challenge_elapsed, clearance_elapsed,
                time.monotonic() - start,
            )
            self._initialized = True
            return

        raise last_error  # type: ignore[misc]  # 循环至少跑一次，必非 None

    def _navigate_and_verify(self, attempt: int) -> tuple[float, float]:
        """导航主站 → 等挑战解开 → 查维护 → 等 clearance。

        返回 ``(挑战耗时, clearance 耗时)``，单位秒，只计本次导航。

        任一环节没过就抛 ``BlockedError``，由 ``ensure_initialized`` 决定是否
        换一次导航重试。``UpstreamMaintenanceError`` 不在重试之列——平台维护
        重试多少次都一样。
        """
        logger.info(
            "CloakBrowser 加载 %s 主站完成 CF 挑战...（第 %d 次）",
            self._profile.name, attempt,
        )
        start = time.monotonic()
        try:
            self._page.goto(
                self._profile.challenge_url,
                wait_until="domcontentloaded",
                timeout=30_000,
            )
        except Exception as e:
            detail = self._describe_navigation_failure(e)
            logger.error("%s 主站加载失败: %s", self._profile.name, detail)
            raise _exc("ScrapeNetworkError")(
                f"{self._profile.name} 主站加载失败{detail}"
            ) from e

        # goto(domcontentloaded) 返回时页面通常还是 CF 挑战页，必须先等挑战
        # 真正解开，再做任何基于页面内容的判定——否则读到的标题和正文是
        # Cloudflare 的，不是 H2S 的。
        self._wait_for_challenge_clear()
        challenge_elapsed = time.monotonic() - start

        # 挑战已解开，此刻的标题/正文才代表站点真实状态
        self._raise_if_maintenance_page()

        # 最后一关：确认 clearance 真的生效。
        # 这里以前等的是 ``[data-cy="FilterList-item"]``——实测它和能否发请求
        # 无关（GraphQL 已经 200 时该元素仍可能没渲染），而且等不到还只告警
        # 就继续，等于把「没通过校验」当成「通过了」。改为直接探 GraphQL：
        # 能拿到响应才算初始化完成。
        clearance_start = time.monotonic()
        self._wait_for_clearance()
        return challenge_elapsed, time.monotonic() - clearance_start

    def _describe_navigation_failure(self, error: Exception) -> str:
        """把 goto() 的异常翻译成一句说得清原因的话。

        Chromium 在代理层失败时只给 ``ERR_TUNNEL_CONNECTION_FAILED`` 这类码，
        看不出是配额耗尽、认证失败还是代理宕机；而默认文案「CF 挑战可能未通过」
        会把排查引向 Cloudflare。命中代理错误码时就直接问代理要真实状态码。

        返回值拼在「主站加载失败」后面，自带括号。
        """
        text = str(error)
        if not any(marker in text for marker in _PROXY_ERROR_MARKERS):
            return f"（CF 挑战可能未通过）: {error}"
        if not self._proxy_url:
            return f"（代理层报错，但本次并未走代理）: {error}"

        from config import probe_proxy  # 延迟导入，避免循环依赖
        from urllib.parse import urlparse

        target = urlparse(self._profile.challenge_url)
        try:
            reason = probe_proxy(
                self._proxy_url,
                target.hostname or "",
                target.port or 443,
            )
        except Exception as e:  # 诊断本身不能盖掉原始错误
            logger.debug("代理探测异常: %s", e)
            return f"（代理层报错，探测代理时又失败了: {e}）: {error}"

        if reason:
            return f"（{reason}）: {error}"
        return f"（代理本身可用，问题在目标站点或链路上）: {error}"

    def _is_challenge_page(self) -> bool:
        """当前页面是否仍是 CF 挑战页。"""
        try:
            return _CF_CHALLENGE_HTML_MARKER in self._page.content()
        except Exception:
            # 页面内容取不到（导航中 / 崩溃）时保守认为仍在挑战，
            # 由 _wait_for_challenge_clear 的超时统一处理。
            return True

    def _wait_for_challenge_clear(
        self, timeout: float = _CHALLENGE_CLEAR_TIMEOUT
    ) -> None:
        """轮询到 CF 挑战解开为止；超时抛 BlockedError。

        这是「挑战是否通过」的唯一判据。以前用 ``[data-cy=FilterList-item]``
        选择器代替，超时后仅告警并继续，导致挑战没过也照发 GraphQL——
        必然 403，再触发会话重建，最终把 source 打进熔断。
        """
        deadline = time.monotonic() + timeout
        while True:
            if not self._is_challenge_page():
                return
            if time.monotonic() >= deadline:
                raise _exc("BlockedError")(
                    f"CF 挑战 {timeout:.0f}s 内未解开，{self._profile.name} "
                    "主站仍停在挑战页。可能需要更换 IP 或等待冷却。"
                )
            time.sleep(_CHALLENGE_POLL_INTERVAL)

    def _raise_if_maintenance_page(self) -> None:
        """挑战解开后，让 profile 的钩子判断站点是否处于维护态。

        必须在 CF 挑战解开后调用：挑战页是 Cloudflare 生成的，其标题和正文
        与站点的真实状态无关，在挑战解开前判定等于拿 CF 的页面去猜站点在
        不在维护。没有配钩子的站点直接跳过。
        """
        check = self._profile.maintenance_check
        if check is None or self._is_challenge_page():
            return

        try:
            title = (self._page.title() or "").strip()
        except Exception:
            title = ""
        try:
            html = self._page.content()[:4000]
        except Exception:
            html = ""

        check(title, html)

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    # ── 通用同源请求 ────────────────────────────────────────────────────
    def _raw_fetch(
        self,
        path: str,
        *,
        method: str = "POST",
        body: str = "",
        headers: Mapping[str, str] | None = None,
        timeout_ms: int = 30_000,
        encrypted: bool | None = None,
    ) -> dict:
        """在页面里发一次同源请求，原样返回 ``{status, ok, text, headers}``。

        不做任何状态码处理，也**不会**触发 ``ensure_initialized``——
        clearance 探测要在初始化过程中调用它，走公开方法会无限递归。

        ``encrypted`` 决定带 body 的请求走不走加密信封：
        - ``None``（默认）→ 跟随 profile 的 ``encrypted_envelope``；
        - ``True`` → 强制信封；``False`` → 强制明文。

        H2S 的 GraphQL 走信封，但同站的 NextAuth 端点（``/api/auth/*``）是明文
        表单 REST，硬套信封只会 400——那条路要显式传 ``encrypted=False``。
        GET/HEAD 没有 body，无所谓信封。
        """
        use_envelope = (
            self._profile.encrypted_envelope if encrypted is None else encrypted
        )
        if use_envelope and method.upper() not in ("GET", "HEAD"):
            return self._encrypted_fetch(
                path, body=body, headers=headers or {}, timeout_ms=timeout_ms,
            )

        merged = dict(self._profile.default_headers)
        if headers:
            merged.update(headers)

        headers_json = _json_dumps(merged)
        body_json = _json_dumps(body)
        path_json = _json_dumps(path)
        method_json = _json_dumps(method.upper())
        # GET / HEAD 不能带 body，带了浏览器直接抛 TypeError
        send_body = method.upper() not in ("GET", "HEAD")

        js_code = f"""
            async () => {{
                const controller = new AbortController();
                const timeout = setTimeout(() => controller.abort(), {timeout_ms});
                try {{
                    const init = {{
                        method: {method_json},
                        credentials: 'include',
                        mode: 'same-origin',
                        cache: 'no-store',
                        redirect: 'follow',
                        referrer: window.location.href,
                        referrerPolicy: 'strict-origin-when-cross-origin',
                        headers: {headers_json},
                        signal: controller.signal,
                    }};
                    if ({'true' if send_body else 'false'}) {{
                        init.body = {body_json};
                    }}
                    const resp = await fetch({path_json}, init);
                    clearTimeout(timeout);
                    const text = await resp.text();
                    const headers = {{}};
                    for (const [key, value] of resp.headers.entries()) {{
                        const lower = key.toLowerCase();
                        if (['cf-ray', 'content-type', 'server', 'vary'].includes(lower)) {{
                            headers[lower] = value;
                        }}
                    }}
                    return {{ status: resp.status, ok: resp.ok, text: text, headers: headers }};
                }} catch (err) {{
                    clearTimeout(timeout);
                    return {{ error: err.message || String(err) }};
                }}
            }}
        """
        return self._page.evaluate(js_code)

    # ── 加密信封传输 ────────────────────────────────────────────────
    #
    # 2026-08-17 起 H2S 的 GraphQL 只走这条路：明文请求会直接吃 Cloudflare
    # 挑战。算法照抄站点自己的 JS（chunk 里搜 ``__enc__``）：
    #
    #     aesKey  = AES-GCM 256，每次请求新生成
    #     k       = RSA-OAEP(SHA-256) 包裹 aesKey 的裸字节
    #     iv      = 12 字节随机数
    #     d       = AES-GCM(aesKey, iv, 明文)
    #     信封     = {v:1, k, iv, d, ct}   全部 base64
    #     POST 到 /api/__enc__，带 x-enc: 1
    #     响应带 x-enc: 1 时，body 是 {iv, d, ct}，用同一个 aesKey 解
    #
    # **为什么在页面里做而不是在 Python 里做**：WebCrypto 就在手边，且密钥
    # 材料不出浏览器；更重要的是这样能沿用同源 fetch 的全部凭据（cookies /
    # clearance / TLS 指纹），与既有的 _raw_fetch 完全同构。
    #
    # 公钥不写死，见 ``_ensure_enc_pubkey``。

    def _ensure_enc_pubkey(self) -> str:
        """取加密用的 RSA 公钥（SPKI base64）。进程内缓存，见 ``_ENC_PUBKEY_CACHE``。

        **按 chunk 名字先筛**（``_ENC_CHUNK_HINTS``），不要遍历全部 script——
        那条路 2026-08-18 把出口 IP 送进了 Cloudflare 黑名单，原因见常量注释。
        """
        cached = _ENC_PUBKEY_CACHE.get(self._profile.name)
        if cached:
            return cached

        key = self._page.evaluate(
            """async ([re, hints]) => {
                const rx = new RegExp(re);
                const all = [...document.querySelectorAll('script[src]')]
                    .map(s => s.src);
                // 先按名字筛；筛不到再退回全量，但正常情况下走不到那一步。
                const named = all.filter(u => hints.some(h => u.includes(h)));
                for (const list of [named, all]) {
                    for (const u of list) {
                        let t;
                        try { t = await (await fetch(u)).text(); }
                        catch (e) { continue; }
                        if (!t.includes('__enc__')) continue;
                        const m = t.match(rx);
                        if (m) return {key: m[1], scanned: list.length,
                                       fallback: list === all};
                    }
                }
                return null;
            }""",
            [_ENC_PUBKEY_RE, list(_ENC_CHUNK_HINTS)],
        )
        if not key or not key.get("key"):
            raise _exc("ScrapeNetworkError")(
                f"{self._profile.name} 找不到加密公钥——bundle 结构可能已变。"
                f"到含 __enc__ 的 chunk 里搜 SPKI 常量（形如 MIIBIjANBgkq…），"
                f"并核对 _ENC_PUBKEY_RE / _ENC_CHUNK_HINTS。"
            )
        if key.get("fallback"):
            # 退回全量说明 _ENC_CHUNK_HINTS 过期了。它能救这一次，但代价正是
            # 当初惹祸的那个行为特征，所以要吵一声，别让它悄悄变成常态。
            logger.warning(
                "%s 按 chunk 名字没找到公钥，退回扫描全部 %d 个 script。"
                "请更新 _ENC_CHUNK_HINTS——长期这么扫会被 Cloudflare 盯上。",
                self._profile.name, key.get("scanned", 0),
            )
        _ENC_PUBKEY_CACHE[self._profile.name] = key["key"]
        logger.info(
            "%s 已取得加密公钥（%d 字符，扫了 %d 个 chunk）",
            self._profile.name, len(key["key"]), key.get("scanned", 0),
        )
        return key["key"]

    def _drop_enc_pubkey(self) -> None:
        """作废缓存的公钥。上游轮换时靠它自愈——下次请求会重新抓。"""
        if _ENC_PUBKEY_CACHE.pop(self._profile.name, None):
            logger.warning("%s 加密公钥已作废，下次请求将重新抓取", self._profile.name)

    def _encrypted_fetch(
        self,
        path: str,
        *,
        body: str,
        headers: Mapping[str, str],
        timeout_ms: int,
    ) -> dict:
        """把 body 包成信封发出去，返回**解密后**的 ``{status, ok, text, headers}``。

        返回形状与 ``_raw_fetch`` 完全一致，所以 ``fetch`` / ``fetch_gql`` 以上
        一行都不用改。

        响应没有 x-enc 头时原样返回明文——403 的
        ``{"code":"clearance_required"}`` 就是这么回的，clearance 探测要靠它。
        """
        pub = self._ensure_enc_pubkey()
        merged = dict(self._profile.default_headers)
        merged.update(headers or {})
        merged[_ENC_HEADER] = "1"
        merged["Content-Type"] = "application/json"

        js_code = """
            async ([pub, path, payload, hdrs, timeoutMs, encHeader]) => {
                const b2a = (b) => { let s = "";
                    for (const x of b) s += String.fromCharCode(x); return btoa(s); };
                const a2b = (s) => { const t = atob(s);
                    const u = new Uint8Array(t.length);
                    for (let i = 0; i < t.length; i++) u[i] = t.charCodeAt(i);
                    return u; };
                const sub = crypto.subtle;
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), timeoutMs);
                try {
                    const rsa = await sub.importKey("spki", a2b(pub),
                        {name: "RSA-OAEP", hash: "SHA-256"}, false, ["encrypt"]);
                    const aes = await sub.generateKey({name: "AES-GCM", length: 256},
                        true, ["encrypt", "decrypt"]);
                    const raw = new Uint8Array(await sub.exportKey("raw", aes));
                    const k = new Uint8Array(
                        await sub.encrypt({name: "RSA-OAEP"}, rsa, raw));
                    const iv = crypto.getRandomValues(new Uint8Array(12));
                    const d = new Uint8Array(await sub.encrypt({name: "AES-GCM", iv},
                        aes, new TextEncoder().encode(payload)));
                    const envelope = {v: 1, k: b2a(k), iv: b2a(iv), d: b2a(d),
                                      ct: "application/json"};

                    const resp = await fetch(path, {
                        method: "POST",
                        credentials: "include",
                        mode: "same-origin",
                        cache: "no-store",
                        redirect: "follow",
                        referrer: window.location.href,
                        referrerPolicy: "strict-origin-when-cross-origin",
                        headers: hdrs,
                        body: JSON.stringify(envelope),
                        signal: controller.signal,
                    });
                    clearTimeout(timer);

                    const raw_text = await resp.text();
                    const out = {};
                    for (const [key, value] of resp.headers.entries()) {
                        const lower = key.toLowerCase();
                        if (['cf-ray', 'content-type', 'server', 'vary',
                             encHeader].includes(lower)) out[lower] = value;
                    }
                    // 没有 x-enc 头 = 明文（403 clearance_required 走这条）
                    if (out[encHeader] !== "1") {
                        return {status: resp.status, ok: resp.ok,
                                text: raw_text, headers: out};
                    }
                    const back = JSON.parse(raw_text);
                    const plain = new Uint8Array(await sub.decrypt(
                        {name: "AES-GCM", iv: a2b(back.iv)}, aes, a2b(back.d)));
                    return {status: resp.status, ok: resp.ok,
                            text: new TextDecoder().decode(plain), headers: out};
                } catch (err) {
                    clearTimeout(timer);
                    return {error: err.message || String(err)};
                }
            }
        """
        result = self._page.evaluate(
            js_code, [pub, path, body, merged, timeout_ms, _ENC_HEADER]
        )
        # JS 侧的 try/catch 把 importKey / encrypt / decrypt 的失败都收成
        # {"error": ...}。公钥轮换正是从这里冒出来的：旧公钥加密的信封服务端
        # 解不开，或响应用新密钥回来我们解不开。作废缓存，下次请求重抓即自愈。
        if isinstance(result, dict) and result.get("error"):
            self._drop_enc_pubkey()
        return result

    def _encrypted_rest_get(
        self,
        path: str,
        *,
        headers: Mapping[str, str],
        timeout_ms: int,
    ) -> dict:
        """GET ``/api/rest/*``：加密**路径**走 ``x-enc-q`` 头。

        照抄站点 module 82361 的函数 ``H``：

            r = url.slice("/api".length)            # "/rest/V1/..."（含 query）
            envelope = encrypt(r, "text/plain")
            headers["x-enc-q"] = base64(JSON(envelope))
            GET /api/rest/__enc__
            响应带 x-enc:1 → 用同一个 aesKey 解密

        注意加密的是**路径字符串**、ct 为 ``text/plain``——与 body 加密那条
        （``application/json``）不是一回事，写混了服务端解不开。
        """
        pub = self._ensure_enc_pubkey()
        merged = dict(self._profile.default_headers)
        merged.update(headers or {})

        inner = path[len(_REST_API_PREFIX):] if path.startswith(_REST_API_PREFIX) else path

        js_code = """
            async ([pub, inner, encPath, hdrs, timeoutMs, encHeader, qHeader]) => {
                const b2a = (b) => { let s = "";
                    for (const x of b) s += String.fromCharCode(x); return btoa(s); };
                const a2b = (s) => { const t = atob(s);
                    const u = new Uint8Array(t.length);
                    for (let i = 0; i < t.length; i++) u[i] = t.charCodeAt(i);
                    return u; };
                const sub = crypto.subtle;
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), timeoutMs);
                try {
                    const rsa = await sub.importKey("spki", a2b(pub),
                        {name: "RSA-OAEP", hash: "SHA-256"}, false, ["encrypt"]);
                    const aes = await sub.generateKey({name: "AES-GCM", length: 256},
                        true, ["encrypt", "decrypt"]);
                    const raw = new Uint8Array(await sub.exportKey("raw", aes));
                    const k = new Uint8Array(
                        await sub.encrypt({name: "RSA-OAEP"}, rsa, raw));
                    const iv = crypto.getRandomValues(new Uint8Array(12));
                    const d = new Uint8Array(await sub.encrypt({name: "AES-GCM", iv},
                        aes, new TextEncoder().encode(inner)));
                    const envelope = {v: 1, k: b2a(k), iv: b2a(iv), d: b2a(d),
                                      ct: "text/plain"};
                    // 站点：x-enc-q = base64(utf8(JSON(envelope)))
                    const q = b2a(new TextEncoder().encode(JSON.stringify(envelope)));
                    const h = Object.assign({}, hdrs);
                    h[qHeader] = q;

                    const resp = await fetch(encPath, {
                        method: "GET",
                        credentials: "include",
                        mode: "same-origin",
                        cache: "no-store",
                        redirect: "follow",
                        referrer: window.location.href,
                        referrerPolicy: "strict-origin-when-cross-origin",
                        headers: h,
                        signal: controller.signal,
                    });
                    clearTimeout(timer);

                    const raw_text = await resp.text();
                    const out = {};
                    for (const [key, value] of resp.headers.entries()) {
                        const lower = key.toLowerCase();
                        if (['cf-ray', 'content-type', 'server', 'vary',
                             encHeader].includes(lower)) out[lower] = value;
                    }
                    if (out[encHeader] !== "1") {
                        return {status: resp.status, ok: resp.ok,
                                text: raw_text, headers: out};
                    }
                    const back = JSON.parse(raw_text);
                    const plain = new Uint8Array(await sub.decrypt(
                        {name: "AES-GCM", iv: a2b(back.iv)}, aes, a2b(back.d)));
                    return {status: resp.status, ok: resp.ok,
                            text: new TextDecoder().decode(plain), headers: out};
                } catch (err) {
                    clearTimeout(timer);
                    return {error: err.message || String(err)};
                }
            }
        """
        result = self._page.evaluate(
            js_code,
            [pub, inner, _REST_ENC_PATH, merged, timeout_ms,
             _ENC_HEADER, _ENC_QUERY_HEADER],
        )
        if isinstance(result, dict) and result.get("error"):
            self._drop_enc_pubkey()
        return result

    def fetch_encrypted_json(
        self,
        path: str,
        *,
        body: str,
        headers: Mapping[str, str] | None = None,
        timeout_ms: int = 30_000,
    ) -> dict:
        """POST 一个加密信封到任意同源路径，返回解密后的 ``{status, ok, text, headers}``。

        给 GraphQL 以外、但同样走信封的站点端点用——目前是 ``/api/booking``
        （站点自己的占房入口，见 docs/H2S_BOOKING_OPS.md §6.10）。编码与站点的
        ``zg`` 一致：``JSON → 信封 → body``，头带 ``x-enc: 1``。

        不做 403 重建：401/403 在这些业务端点上是语义响应（未登录、参数无效），
        换 IP 解决不了。状态码原样交回调用方。
        """
        self.ensure_initialized()
        result = self._encrypted_fetch(
            path, body=body, headers=headers or {}, timeout_ms=timeout_ms,
        )
        if "error" in result:
            raise _exc("ScrapeNetworkError")(
                f"{self._profile.name} 加密请求失败 {path}: {result['error']}"
            )
        return result

    def fetch_rest(
        self,
        path: str,
        *,
        method: str = "GET",
        body: str = "",
        headers: Mapping[str, str] | None = None,
        timeout_ms: int = 30_000,
    ) -> dict:
        """发一次 ``/api/rest/*`` 请求，按站点的信封约定编码，返回解密后的响应。

        GET 走 ``x-enc-q``（加密路径），其余走加密 body（形状同 GraphQL）。
        两条规则逐字照抄自站点 module 82361 的 ``H`` / ``J``，见
        docs/H2S_BOOKING_OPS.md §6.9。

        **不是 ``fetch_plain``**：那条是给 ``/api/auth/*``（NextAuth）用的，
        站点对它确实不加密（拦截器只对 ``/api/rest/`` 生效）。两者别混。
        """
        self.ensure_initialized()
        if method.upper() == "GET":
            result = self._encrypted_rest_get(
                path, headers=headers or {}, timeout_ms=timeout_ms,
            )
        else:
            result = self._encrypted_fetch(
                path, body=body, headers=headers or {}, timeout_ms=timeout_ms,
            )
        if "error" in result:
            raise _exc("ScrapeNetworkError")(
                f"{self._profile.name} REST 请求失败 {path}: {result['error']}"
            )
        return result

    def _raw_fetch_gql(
        self,
        query: str,
        variables: dict | None = None,
        *,
        operation_name: str = "",
        timeout_ms: int = 30_000,
        extra_headers: dict[str, str] | None = None,
    ) -> dict:
        """GraphQL 形态的 ``_raw_fetch``。"""
        return self._raw_fetch(
            _H2S_GQL_PATH,
            method="POST",
            body=_gql_body(query, variables, operation_name),
            headers=extra_headers,
            timeout_ms=timeout_ms,
        )

    def _is_clearance_required(self, result: dict) -> bool:
        """403 是否属于「clearance 还没落地」这种**瞬时**状态。

        两者都是 403，但含义相反：clearance 未生效重新导航就好，IP 被封则
        换 IP 才有用。混为一谈会把马上要生效的会话反复推倒重建。

        判据由 profile 给：
        - H2S 回自己的 JSON ``{"code":"clearance_required"}``
        - 多数站点直接回 CF 挑战页（``_cf_chl_opt`` / ``Just a moment``）
        """
        if result.get("status") != 403:
            return False
        text = (result.get("text") or "").lower()
        return any(
            marker.lower() in text
            for marker in self._profile.clearance_pending_markers
        )

    def _wait_for_clearance(self, timeout: float = _CLEARANCE_TIMEOUT) -> None:
        """轮询到 GraphQL 不再回 clearance_required 为止。

        挑战页消失只说明文档被真实页面替换了，``cf_clearance`` cookie 未必
        已经生效——实测两者之间有约 2s 的空窗，这期间发请求必然 403。
        真正的「初始化完成」是这个探测通过。
        """
        probe = self._profile.clearance_probe
        if probe is None:
            # 没配探针的站点跳过：由首个真实请求遇到 403 时重新导航兜底
            return

        deadline = time.monotonic() + timeout
        while True:
            try:
                result = self._raw_fetch(
                    probe.path,
                    method=probe.method,
                    body=probe.body,
                    headers=probe.headers,
                    timeout_ms=15_000,
                )
            except Exception as e:
                # 页面还在动（导航 / 重绘）时 evaluate 可能直接抛，等下一轮
                logger.debug("clearance 探测异常，重试: %s", e)
                result = {"error": str(e)}

            if "error" not in result and not self._is_clearance_required(result):
                return
            if time.monotonic() >= deadline:
                raise _exc("BlockedError")(
                    f"CF clearance {timeout:.0f}s 内未生效，{self._profile.name} "
                    "持续要求浏览器校验。可能需要更换 IP 或等待冷却。"
                )
            time.sleep(_CLEARANCE_POLL_INTERVAL)

    def fetch_gql(
        self,
        query: str,
        variables: dict | None = None,
        *,
        operation_name: str = "",
        timeout_ms: int = 30_000,
        extra_headers: dict[str, str] | None = None,
    ) -> dict:
        """
        在浏览器内通过 fetch() 发 GraphQL POST 请求。

        Parameters
        ----------
        query         : GraphQL query 或 mutation 字符串
        variables     : GraphQL variables dict（可选）
        operation_name: GraphQL operationName。H2S 自 2026-08-18 起按 operation
                        白名单放行，缺了它一律 403 ``operation_not_allowed``
        timeout_ms    : fetch 超时毫秒数
        extra_headers : 额外 HTTP 头（e.g. Authorization: Bearer xxx）

        Returns
        -------
        响应 JSON 的完整 dict（含 data / errors 字段）

        Raises
        ------
        BlockedError              HTTP 403 且是 Cloudflare 屏蔽（换 IP 有意义）
        OperationNotAllowedError  HTTP 403 但是 operation 没登记（换 IP 无意义）
        RateLimitError            HTTP 429 (限流)
        ScrapeNetworkError        网络/超时错误 / 非 JSON 响应
        """
        try:
            result = self.fetch(
                _H2S_GQL_PATH,
                method="POST",
                body=_gql_body(query, variables, operation_name),
                headers=extra_headers,
                timeout_ms=timeout_ms,
            )
        except _exc("OperationNotAllowedError") as e:
            # fetch() 是站点通用入口，看不到 operationName。把它补进消息里——
            # 这条异常唯一的修法就是「照抄哪条 operation」，不写清楚是哪条，
            # 日志等于只说了「有一条不行」，而 booker 里有 9 条。
            raise _exc("OperationNotAllowedError")(
                f"operation {operation_name or _operation_label(query)} 被拒: {e}"
            ) from e

        import json

        try:
            return json.loads(result["text"])
        except json.JSONDecodeError as e:
            raise _exc("ScrapeNetworkError")(
                f"{self._profile.name} 响应非 JSON: {e}"
            ) from e

    def fetch_form(
        self,
        path: str,
        data: Mapping[str, str],
        *,
        timeout_ms: int = 30_000,
        headers: Mapping[str, str] | None = None,
    ) -> dict:
        """发一次 ``application/x-www-form-urlencoded`` POST，返回解析后的 JSON。

        给 WordPress admin-ajax 这类表单端点用（Xior）。
        """
        from urllib.parse import urlencode

        result = self.fetch(
            path,
            method="POST",
            body=urlencode(dict(data)),
            headers=headers,
            timeout_ms=timeout_ms,
        )

        import json

        try:
            return json.loads(result["text"])
        except json.JSONDecodeError as e:
            raise _exc("ScrapeNetworkError")(
                f"{self._profile.name} 响应非 JSON: {e}"
            ) from e

    def fetch_plain(
        self,
        path: str,
        *,
        method: str = "GET",
        body: str = "",
        headers: Mapping[str, str] | None = None,
        timeout_ms: int = 30_000,
    ) -> dict:
        """发一次**不走加密信封**的同源请求，原样返回 ``{status, ok, text, headers}``。

        给同站的明文 REST 端点用——目前是 H2S 的 NextAuth 登录流
        （``/api/auth/csrf`` · ``/api/auth/callback/credentials`` ·
        ``/api/auth/session``）。这些不是 GraphQL、不吃加密信道，被 ``fetch()``
        默认套上信封只会 400。

        与 ``fetch()`` 的两点区别，都是刻意的：

        - **强制明文**（``encrypted=False``）——绕开 profile 的
          ``encrypted_envelope``；
        - **不做 403/CF 重建**——登录失败回 401、需要重新校验回 403，都是业务
          语义的响应，换 IP / 重建浏览器解决不了。状态码原样交回调用方判断。

        仍然会 ``ensure_initialized``：NextAuth 端点同样在 Cloudflare 后面，
        clearance cookie 得先就位。
        """
        self.ensure_initialized()
        result = self._raw_fetch(
            path, method=method, body=body, headers=headers,
            timeout_ms=timeout_ms, encrypted=False,
        )
        if "error" in result:
            raise _exc("ScrapeNetworkError")(
                f"{self._profile.name} 明文请求失败 {path}: {result['error']}"
            )
        return result

    def fetch(
        self,
        path: str,
        *,
        method: str = "POST",
        body: str = "",
        headers: Mapping[str, str] | None = None,
        timeout_ms: int = 30_000,
    ) -> dict:
        """发一次同源请求并处理 CF 相关的失败，返回 ``{status, ok, text, headers}``。

        这是所有站点共用的请求入口：``fetch_gql`` / ``fetch_form`` 只是在它
        外面套一层响应解析。

        Raises
        ------
        BlockedError              403 且重建会话后仍被挡
        OperationNotAllowedError  403 且正文表明这条 operation 没在白名单里。
                                  **不重建、不换 IP** —— 见该异常的 docstring
        RateLimitError            429
        ScrapeNetworkError        网络/超时错误
        UpstreamMaintenanceError  重建过程中发现站点在维护
        """
        self.ensure_initialized()
        result = self._raw_fetch(
            path, method=method, body=body, headers=headers, timeout_ms=timeout_ms
        )
        if "error" in result:
            raise _exc("ScrapeNetworkError")(f"浏览器内 fetch 失败: {result['error']}")

        status = result["status"]

        # clearance 过期是长会话里的常态（token 有寿命，且实测生产环境的
        # token 比本地短得多），不是被封。
        #
        # 恢复方式只有一种：重新导航主站把挑战跑完——token 是页面通过挑战时
        # 下发的，对着 API 轮询永远换不出新 token，只会白等到超时再误判成
        # 屏蔽。ensure_initialized() 里的 _wait_for_clearance 之所以有效，
        # 是因为那里刚做完 goto，等的是 cookie 落地而不是 token 重签。
        if self._is_clearance_required(result):
            logger.info(
                "%s 要求重新校验，重新走主站挑战流程...", self._profile.name
            )
            self._initialized = False
            self.ensure_initialized()
            result = self._raw_fetch(
                path, method=method, body=body, headers=headers, timeout_ms=timeout_ms
            )
            if "error" in result:
                raise _exc("ScrapeNetworkError")(
                    f"clearance 恢复后重试失败: {result['error']}"
                )
            status = result["status"]

        if status == 403 and _is_operation_rejected(result.get("text", "")):
            # 403 但不是 Cloudflare —— 是上游应用说「这条 operation 没登记」。
            #
            # 必须在下面那段重建之前拦住。重建做的三件事（换出口 IP、换指纹、
            # 重跑 CF 挑战）对这种 403 一件都不管用：正文由业务后端生成，
            # Cloudflare 只是把它转出来。2026-08-19 一次自动预订就是这样烧掉
            # 75 秒和两轮完整挑战，最后拿到的还是同一个 403，并且把误判上抛成
            # BlockedError，触发了 1 小时登录链路抑制。
            #
            # 抛一个**不继承 BlockedError**的异常：上层任何 `except BlockedError`
            # 都不该接住它，否则换 IP / 熔断 / 抑制会原样重演。
            raise _exc("OperationNotAllowedError")(
                f"{self._profile.name} 拒绝了这条 GraphQL operation："
                f"不在上游白名单里（HTTP 403，正文由业务后端返回，非 Cloudflare 挑战页）。"
                f"换 IP、重建浏览器、等冷却都无效，只能照抄站点自己发的那条 operation。"
                f"路径 {path}，响应: {result.get('text', '')[:200]}"
            )

        if status == 403:
            logger.warning(
                "%s 返回 403，尝试重建 CF 会话... headers=%s body=%s",
                self._profile.name,
                result.get("headers", {}),
                result.get("text", "")[:300],
            )
            # 走到这里说明不是 clearance 过期（那条路在上面已经处理并重试过），
            # 是这个出口 IP 被挡了。原地重跑挑战换不掉 IP，先换浏览器。
            self._initialized = False
            self._rebuild_browser()
            try:
                self.ensure_initialized()
            except _exc("UpstreamMaintenanceError"):
                # 重建时发现平台在维护：这是维护，不是屏蔽。压成 BlockedError
                # 会让 monitor 走熔断 + admin 告警，而不是安静的维护冷却。
                raise
            except Exception as e:
                raise _exc("BlockedError")(
                    f"{self._profile.name} 返回 403，CF 会话重建失败。"
                    "可能需要更换 IP 或等待冷却。"
                ) from e
            retry = self._raw_fetch(
                path, method=method, body=body, headers=headers, timeout_ms=timeout_ms
            )
            if "error" in retry:
                raise _exc("ScrapeNetworkError")(f"重建后重试失败: {retry['error']}")
            if retry["status"] == 403:
                logger.warning(
                    "%s 重建会话后仍返回 403 headers=%s body=%s",
                    self._profile.name,
                    retry.get("headers", {}),
                    retry.get("text", "")[:300],
                )
                # 重建后才露出 operation 文案的情形也要认下来。上面那道闸门只看
                # 第一次响应，而挑战未过时拿到的是挑战页 HTML，真正的业务 403
                # 要到挑战过了之后才看得见。
                if _is_operation_rejected(retry.get("text", "")):
                    raise _exc("OperationNotAllowedError")(
                        f"{self._profile.name} 拒绝了这条 GraphQL operation："
                        f"不在上游白名单里。重建会话后仍是同一个 403 —— 与出口 IP 无关。"
                        f"路径 {path}，响应: {retry.get('text', '')[:200]}"
                    )
                raise _exc("BlockedError")(
                    f"{self._profile.name} 持续返回 403。可能需要更换 IP 或等待冷却。"
                )
            result = retry
            status = result["status"]

        if status == 429:
            raise _exc("RateLimitError")(
                f"{self._profile.name} 返回 429 Too Many Requests"
            )

        if status == 404:
            # 404 = 这个路径在上游不存在了，几乎只有一个成因：端点被迁走。
            #
            # 单独拎出来是因为它此前落进下面那条通用分支，报成
            # 「抓取网络失败 … 请检查代理/网络」——2026-08-11 H2S 把
            # /api/graphql 迁到 /api/service/residences 时，日志连刷三天
            # 「请检查代理/网络」，而代理一直是好的。诊断被引向了完全错误的
            # 方向，是那次静默三天的直接原因之一。
            #
            # 仍然抛 ScrapeNetworkError：调用方的隔离与重试语义不该变，改的
            # 只是这句话指向哪儿。
            raise _exc("ScrapeNetworkError")(
                f"{self._profile.name} HTTP 404 —— 端点 {path} 不存在，"
                f"上游很可能改了 API 路径（不是代理或网络问题，别往那个方向查）。"
                f"响应: {result['text'][:200]}"
            )

        if not result["ok"] and status >= 400:
            raise _exc("ScrapeNetworkError")(
                f"{self._profile.name} HTTP {status}: {result['text'][:300]}"
            )

        return result
