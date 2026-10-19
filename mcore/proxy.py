"""代理池、故障切换与 TLS 指纹池。

从 config.py 拆出。这里只有网络出口策略，不依赖 Config / load_config；旧调用点
仍可从 config 导入（config 保留了 re-export）。

代理连续确认故障后进 cooldown；全部代理都在冷却时返回空串，抓取降级为直连
服务器原生 IP。
"""
from __future__ import annotations

import hashlib as _hashlib
import itertools as _itertools
import os
import re
import re as _re
import time as _time
from typing import Optional
from urllib.parse import (
    quote as _quote,
    unquote as _unquote,
    urlparse as _urlparse,
    urlunparse as _urlunparse,
)

from env_utils import env_int


# 配合代理使用时每个 IP 随机选取不同指纹，模拟真实多用户浏览器分布。
# 池中指纹均来自 curl_cffi 支持的现代浏览器版本。
#
# 多元化思路
# ----------
# 旧池只有 Chrome × 2 + Safari + Edge，TLS 栈集中在 BoringSSL 系。新池
# 加入 Firefox（NSS 栈）和移动端（iOS Safari / Android Chrome），让
# Cloudflare 看到的"浏览器分布"更接近真实流量直方图。
#
# 出现连续 Connection closed abruptly 时可更新或扩充列表。
#
# **用它的是不挑指纹的平台**：Plaza、Magis、Student Experience 的抓取与 Plaza
# 预订，以及通知渠道的 HTTP 会话。2026-09-15 实测这三个平台对 curl_cffi 0.16.3
# 的全部候选都是 200，所以这里按「每个家族取最新的」来选。
#
# SecureRC（OurDomain / OurCampus / Xior 的 floorplans.aspx）**不用这个池**，
# 它此刻一律拒绝桌面 Chrome，有自己按实测挑的池——见
# ``scrapers.ourdomain._DEFAULT_IMPERSONATES``。
#
# edge101 移出：curl_cffi 里 Edge 只到 101（2022 年），留着只会是池里最显眼的
# 过时指纹。所有名字由 tests/test_impersonate_pools.py 核对是否为 curl_cffi
# 真实支持的 target——写错一个字母，请求会在运行时才炸。
_CURL_IMPERSONATE_POOL = [
    "chrome150",          # Chrome 150（桌面，curl_cffi 0.16.3 最新）
    "chrome146",          # Chrome 146
    "safari2601",         # macOS Safari 26.0.1
    "safari184",          # macOS Safari 18.4
    "safari260_ios",      # iOS Safari 26（移动端，TLS 与 macOS 不同）
    "firefox147",         # Firefox 147（NSS 栈，与 Chromium 系完全不同）
    "firefox144",         # Firefox 144
    "chrome131_android",  # Android Chrome 131（curl_cffi 里最新的 Android）
]
# Chrome 桌面 40% / Safari 30% / Firefox 20% / Android 10%。
_POOL_WEIGHTS = [3, 1, 2, 1, 1, 1, 1, 1]
assert len(_POOL_WEIGHTS) == len(_CURL_IMPERSONATE_POOL)

_last_impersonate: Optional[str] = None


# ── 代理池 + 故障切换 ───────────────────────────────────────────────
#
# 主代理（HTTPS_PROXY/HTTP_PROXY/ALL_PROXY）+ 备用代理（SCRAPE_PROXIES_FALLBACK，
# 逗号/换行分隔多个）。主代理挂了（webshare 502 之类）自动切到下一个可用的。
#
# 代理连续确认故障后进 cooldown（默认 10 min），期间 get_proxy_url 跳过它；
# 冷却结束后自动重新纳入候选。若所有代理都在 cooldown，则抓取降级为直连
# 服务器原生 IP；monitor 会把轮询频率降到最多 10 min 一次，避免原生 IP
# 被快速打穿。
# 状态进程级，monitor 重启清零（重启即重新从主代理试）。

_PROXY_COOLDOWN_SEC = 600  # 10 分钟

#: 账户级故障（402 欠费 / 407 认证失败）的冷却时长，默认 1 小时。
#:
#: 为什么要和 10 分钟分开：账户级故障**不会自己好**，10 分钟一到就把死代理放回
#: 候选，等于每小时自伤六次。2026-08-26 生产实况——webshare 两个账号同时 402，
#: 每 10 分钟 Xior 和 OurCampus 就各丢一轮，而 H2S 因为浏览器一建用 2 小时反而
#: 躲过了（``_BROWSER_MAX_AGE``），同一次故障在三个 source 上表现完全不同。
#:
#: 为什么是 1 小时而不是更长：判错的代价是「一个其实已经充上值的代理白等」。
#: 10 分钟 → 1 小时已经把自伤从每小时 6 次压到 1 次，再往上（比如 4 小时）边际
#: 收益只有 0.75 次/小时，而恢复延迟涨 4 倍。不划算。
#:
#: 等不及的话重启进程即可——冷却状态是进程级的，重启就清零（见本节开头注释）。
#: 充完值重启容器是最快的恢复路径。
def _env_int_positive(name: str, default: int) -> int:
    """读一个正整数环境变量；空/非法/非正一律回落默认值。"""
    return env_int(name, default, positive=True)


_PROXY_ACCOUNT_COOLDOWN_SEC = _env_int_positive("PROXY_ACCOUNT_COOLDOWN_SEC", 3600)
_PROXY_FAILURE_CONFIRM_THRESHOLD = 2
_PROXY_FAILURE_CONFIRM_WINDOW_SEC = 600
_proxy_cooldown_until: dict[str, float] = {}  # proxy_url -> monotonic 截止
_proxy_failure_marks: dict[str, tuple[int, float]] = {}  # proxy_url -> (count, first_seen)


def _proxy_pool() -> list[str]:
    """主代理 + 备用代理，去重保序，去空。"""
    primary = (
        os.environ.get("HTTPS_PROXY", "")
        or os.environ.get("HTTP_PROXY", "")
        or os.environ.get("ALL_PROXY", "")
    ).strip()
    fallback_raw = os.environ.get("SCRAPE_PROXIES_FALLBACK", "")
    pool = [primary] + [p.strip() for p in re.split(r"[,\n]", fallback_raw)]
    return list(dict.fromkeys(p for p in pool if p))  # 去重保序 + 去空


# webshare sticky 端点的用户名形如 ``{user}-{country}-{session_id}``，
# 末段是纯数字的 session id；``-rotate`` 则表示每请求换 IP。
_STICKY_SESSION_RE = _re.compile(r"^(?P<head>.+)-(?P<session>\d+)$")


_rotating_counter = _itertools.count(1)


def _derive_session_id(source: str, rotating: bool = False) -> str:
    """由 source 名派生 6 位 session id。

    ``rotating=False``（默认）——同一个 source 每次都得到同一个 id，出口 IP
    因此稳定，Cloudflare clearance 才能复用。H2S / Xior 用这个。

    ``rotating=True``——每次调用都换一个新 id，即每次拿到不同出口 IP。
    给**不依赖 clearance、反而依赖换 IP 来解 403** 的 source 用（OurDomain）。
    """
    if rotating:
        return str(100000 + next(_rotating_counter) * 7919 % 900000)
    digest = _hashlib.sha1(source.encode("utf-8")).hexdigest()
    return str(100000 + int(digest[:8], 16) % 900000)


def _with_source_session(url: str, source: str, rotating: bool = False) -> str:
    """把代理 URL 的 sticky session id 换成该 source 专属的。

    只在用户名**已经**以数字 session id 结尾时才替换；其它形态（如
    ``-rotate``、或根本没有 session 段）原样返回——凭空拼接可能被
    webshare 解析成国家码之类，反而把配置搞坏。
    """
    try:
        parsed = _urlparse(url)
        username = _unquote(parsed.username or "")
        password = parsed.password or ""
    except Exception:
        return url
    if not username or not parsed.hostname:
        return url

    m = _STICKY_SESSION_RE.match(username)
    if not m:
        return url  # rotate 端点或无 session 段：保持原样

    new_user = f"{m.group('head')}-{_derive_session_id(source, rotating)}"
    netloc = f"{_quote(new_user, safe='')}:{password}@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return _urlunparse(parsed._replace(netloc=netloc))


def _personal_proxies() -> list[str]:
    """``SCRAPE_PROXIES_PERSONAL``：池子里哪些代理是「自己的线路」。

    典型是家里那条反向 SSH 隧道——它不是买来的商业住宅池，而是用户自家的宽带
    和自家的 IP。把它排在最后（见 SCRAPE_PROXIES_FALLBACK 的顺序）只解决「什么
    时候用」，不解决「用多狠」：一旦用上，抓取节奏和平时一样是高峰 20–60 秒
    一轮，全部打在同一个住宅 IP 上。

    2026-08-02 的教训正好说明这有多危险：所有 source 挤在同一个 sticky 出口时，
    Xior 一轮 12 个请求就触发 429，四栋楼全挂。那还只是商业池里的一个出口，
    烧了换一个就是；自家 IP 烧了没得换，而且影响的是家里所有上网。

    所以这是**主动降速**，不是故障降级——两者的判据和目的都不同，代码里也分开。
    """
    raw = os.environ.get("SCRAPE_PROXIES_PERSONAL", "")
    return [p.strip() for p in re.split(r"[,\n]", raw) if p.strip()]


def is_personal_proxy_active() -> bool:
    """当前生效的代理是不是「自己的线路」。

    比的是**当前会用哪个**（``get_proxy_url()`` 的返回），不是「池子里有没有」——
    商业代理还活着的时候不该限速。

    比较前先剥掉 sticky session 改写：``_with_source_session`` 会把用户名换成
    per-source 的 id，直接字符串相等会漏。个人代理通常没有凭据（``socks5://ip:port``）
    因而不受改写影响，但不能依赖这一点。
    """
    personal = _personal_proxies()
    if not personal:
        return False
    current = get_proxy_url()
    if not current:
        return False
    return any(_same_proxy_endpoint(current, p) for p in personal)


def _same_proxy_endpoint(a: str, b: str) -> bool:
    """两个代理 URL 是否指向同一个端点（忽略用户名里的 session 段与密码）。"""
    try:
        pa, pb = _urlparse(a), _urlparse(b)
    except Exception:
        return a == b
    return (pa.scheme, pa.hostname, pa.port) == (pb.scheme, pb.hostname, pb.port)


def get_proxy_url(source: str = "", *, rotating: bool = False) -> str:
    """
    统一的代理 URL 读取，**带故障切换**。

    返回当前**未在冷却**的第一个代理（主代理优先；主代理被
    ``report_proxy_failure`` 标记故障后自动落到备用）。全部都在冷却时返回
    空串，表示抓取临时降级为直连服务器原生 IP。无配置也返回空串。

    所有需要代理的模块（scraper、booker、monitor）均通过此函数获取。

    Parameters
    ----------
    source
        传入 source 名（``holland2stay`` / ``xior`` / ``ourdomain``）时，为该
        source 派生**独立的 sticky session**，使各平台拥有各自稳定的出口 IP。

        为什么要分开：出口 IP 稳定是 Cloudflare clearance 能复用的前提，但共用
        一个 IP 就等于共用限流额度。2026-08-02 实测——所有 source 挤在同一个
        sticky IP 上时，Xior 一轮 12 个请求触发 `429 Too Many Requests`，四栋楼
        全部失败。各自独立 session 后两者兼得：IP 稳定，额度不互相挤占。

        不传则返回基础 URL（monitor / doctor 只判断有没有配代理，用这个即可）。
    rotating
        每次调用换一个新 session（即换出口 IP）。给**不依赖 clearance、反而
        依赖换 IP 来解 403** 的 source 用。

        OurDomain 就是这种：它没有 clearance 可复用，抗封手段是轮换 TLS 指纹。
        但那套机制此前是搭了「每请求换 IP」的便车——2026-08-02 把它固定到专属
        sticky IP 后，同一个 IP 被 CF 盯上时四个指纹轮完全部 403，无法自愈。
    """
    pool = _proxy_pool()
    if not pool:
        return ""
    now = _time.monotonic()
    for p in pool:
        if _proxy_cooldown_until.get(p, 0.0) <= now:
            return _with_source_session(p, source, rotating) if source else p
    # 全在冷却——降级为直连原生 IP；monitor 会降频到最多 10 min 一次。
    return ""


# CONNECT 被拒时代理给的状态码，及其真实含义。Chromium 不会把这些码透出来，
# 它对任何代理层失败都只报 ERR_TUNNEL_CONNECTION_FAILED。
_PROXY_REJECT_HINTS: dict[int, str] = {
    402: "流量配额耗尽或账户欠费",
    403: "该出口被代理商禁用",
    407: "代理认证失败，用户名或密码不对",
    429: "代理侧限流",
    502: "代理无法连到目标站点",
    503: "代理服务暂时不可用",
}


def probe_proxy(proxy_url: str, host: str, port: int = 443, timeout: float = 8.0) -> str | None:
    """向代理发一次 CONNECT，确认它到底能不能用。

    代理正常（回 200）时返回 ``None``；否则返回一句可直接写进日志的原因。

    存在的理由是 Chromium 的错误码没有信息量：配额耗尽（402）、认证失败
    （407）、代理进程宕机，到了 Playwright 那层一律是
    ``ERR_TUNNEL_CONNECTION_FAILED``。2026-08-05 线上代理欠费停服，日志里
    六百多行全写着「CF 挑战可能未通过」，实际和 Cloudflare 毫无关系。

    这里直接跟代理说话，把它自己给的状态码取回来。凭据只用于构造
    ``Proxy-Authorization`` 头，不进返回值也不进日志。
    """
    import base64
    import socket

    if not proxy_url:
        return None
    try:
        parsed = _urlparse(proxy_url)
        proxy_host = parsed.hostname
        proxy_port = parsed.port or 80
    except Exception:
        return None
    if not proxy_host:
        return None

    target = f"{host}:{port}"
    lines = [f"CONNECT {target} HTTP/1.1", f"Host: {target}"]
    if parsed.username:
        cred = f"{_unquote(parsed.username)}:{_unquote(parsed.password or '')}"
        token = base64.b64encode(cred.encode("utf-8")).decode("ascii")
        lines.append(f"Proxy-Authorization: Basic {token}")
    request = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")

    sock = None
    try:
        sock = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
        sock.settimeout(timeout)
        sock.sendall(request)
        # 状态行就在第一个 CRLF 之前，读一小段足够，不必等完整响应头
        raw = sock.recv(256).decode("latin-1", "replace")
    except OSError as e:
        return f"连不上代理 {proxy_host}:{proxy_port}（{type(e).__name__}: {e}）"
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    status_line = raw.split("\r\n", 1)[0].strip()
    m = re.match(r"HTTP/\d(?:\.\d)?\s+(\d{3})(?:\s+(.*))?", status_line)
    if not m:
        return f"代理返回无法解析的响应: {status_line[:80]!r}"
    code = int(m.group(1))
    if code == 200:
        return None
    reason = (m.group(2) or "").strip()
    hint = _PROXY_REJECT_HINTS.get(code)
    detail = f"{code} {reason}".strip()
    return f"代理拒绝 CONNECT: {detail}（{hint}）" if hint else f"代理拒绝 CONNECT: {detail}"


def is_proxy_native_fallback_active() -> bool:
    """
    是否因所有已配置代理都在冷却中而进入直连 fallback。

    注意无代理配置不算 fallback；那是用户主动选择直连。
    """
    pool = _proxy_pool()
    if not pool:
        return False
    now = _time.monotonic()
    return all(_proxy_cooldown_until.get(p, 0.0) > now for p in pool)


def report_proxy_failure(
    url: str = "",
    *,
    service_error_confirmed: bool = True,
    account_level: bool = False,
) -> str:
    """
    记录一次代理故障。``url`` 留空时记录**当前选中**的那个（即刚刚用过、
    刚失败的那个）。

    只有同一代理在确认窗口内连续失败达到阈值，且本次错误已确认是代理
    服务端异常时，才把它放入 cooldown。返回下一轮 get_proxy_url 会用的
    代理；若所有代理都进入冷却则返回空串，表示下一轮将直连服务器原生 IP。

    ``account_level=True``（402 欠费 / 407 认证失败，见
    ``scrapers.base.is_proxy_account_error``）改用 ``_PROXY_ACCOUNT_COOLDOWN_SEC``
    这档长冷却——那种故障不会自己好，按 10 分钟回去重试只是反复自伤。
    """
    pool = _proxy_pool()
    target = url.strip() or (pool[0] if pool else "")
    # 标记当前选中的（不是 pool[0]，因为 pool[0] 可能已在冷却）
    if not url:
        target = get_proxy_url()
    if target and _record_proxy_failure_mark(target) and service_error_confirmed:
        # 账户级故障用长冷却：它不会自己好，10 分钟就回去敲等于每小时自伤六次。
        cooldown = (
            _PROXY_ACCOUNT_COOLDOWN_SEC if account_level else _PROXY_COOLDOWN_SEC
        )
        _proxy_cooldown_until[target] = _time.monotonic() + cooldown
        _proxy_failure_marks.pop(target, None)
    return get_proxy_url()


def is_proxy_in_cooldown(url: str) -> bool:
    """指定代理是否已进入 cooldown。"""
    if not url:
        return False
    return _proxy_cooldown_until.get(url, 0.0) > _time.monotonic()


def proxy_failure_mark_count(url: str) -> int:
    """指定代理当前确认窗口内的失败标记次数，用于日志/测试。"""
    if not url:
        return 0
    count, first_seen = _proxy_failure_marks.get(url, (0, 0.0))
    if first_seen and _time.monotonic() - first_seen <= _PROXY_FAILURE_CONFIRM_WINDOW_SEC:
        return count
    return 0


def _record_proxy_failure_mark(url: str) -> bool:
    """记录一次故障标记；达到确认阈值返回 True。"""
    now = _time.monotonic()
    count, first_seen = _proxy_failure_marks.get(url, (0, 0.0))
    if not first_seen or now - first_seen > _PROXY_FAILURE_CONFIRM_WINDOW_SEC:
        count = 0
        first_seen = now
    count += 1
    _proxy_failure_marks[url] = (count, first_seen)
    return count >= _PROXY_FAILURE_CONFIRM_THRESHOLD


def proxy_pool_size() -> int:
    """配置的代理总数（主 + 备）。0=没配代理，1=只有主代理（无备用）。"""
    return len(_proxy_pool())


def get_impersonate() -> str:
    """从指纹池中随机选取一个 TLS 指纹（避免连续两次选同一个）。"""
    import random
    global _last_impersonate
    pool = list(_CURL_IMPERSONATE_POOL)
    weights = list(_POOL_WEIGHTS)
    # 如果上次选的值在池中且池大小 > 1，排除上次值并同步移除对应权重
    if _last_impersonate is not None and _last_impersonate in pool and len(pool) > 1:
        idx = pool.index(_last_impersonate)
        pool.pop(idx)
        if idx < len(weights):
            weights.pop(idx)
    choice = random.choices(pool, weights=weights, k=1)[0]
    _last_impersonate = choice
    return choice
