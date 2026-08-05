"""
config.py — 全局配置与过滤条件
================================
职责
----
1. 定义全局运行参数（轮询间隔、监控城市、数据库路径、日志级别、智能轮询）
2. 提供 `ListingFilter` / `AutoBookConfig` dataclass，供 users.py 引用
3. `load_config()` 从 .env / 环境变量读取并构造 `Config` 实例

分层说明
--------
- **全局配置**（Config）：影响整个进程，存于 .env，在 Web 面板「全局设置」页修改
- **用户级配置**（ListingFilter / AutoBookConfig）：每用户独立，存于 SQLite user_configs，
  在 Web 面板「用户管理」页修改

依赖关系
--------
仅依赖标准库和 python-dotenv，无内部模块依赖。
users.py 和 web.py 都会 import 本模块中的 dataclass。
"""
from __future__ import annotations

import logging
import os
import re
import sys

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from dotenv import load_dotenv

from models import canonical_feature, parse_float, parse_int


# 已知能耗等级白名单（大写），按优→差排序
ENERGY_LABELS = ["A+++", "A++", "A+", "A", "B", "C", "D", "E", "F"]


def energy_rank(label: str) -> int | None:
    """
    能耗等级 → 数值排名（越小越好）。
    仅接受白名单中的标签（精确匹配，大小写不敏感）；
    未知标签返回 None。
    """
    if not isinstance(label, str):
        return None
    upper = label.strip().upper()
    try:
        return ENERGY_LABELS.index(upper)
    except ValueError:
        return None


if TYPE_CHECKING:
    from models import Listing

logger = logging.getLogger(__name__)

if getattr(sys, "frozen", False):
    # 持久化数据存放到用户目录，保证 web 和 monitor 进程共享同一份数据
    BASE_DIR = Path.home() / ".h2s-monitor"
    ASSETS_DIR = Path(sys._MEIPASS).resolve()
else:
    BASE_DIR = Path(__file__).resolve().parent
    ASSETS_DIR = BASE_DIR

DATA_DIR = BASE_DIR / "data"
ENV_PATH = BASE_DIR / ".env"


def write_env_key(key: str, value: str) -> None:
    """
    写入或更新 .env 文件中的单个键值对（不使用原子 rename）。

    dotenv.set_key() 内部调用 os.replace()（原子 rename），在 Docker
    bind-mount 的 .env 文件上会触发 OSError [Errno 16] Device or resource busy。
    本函数直接读取 → 内存修改 → 原地写回，绕过该限制。

    供 web.py / crypto.py 共享使用，避免重复实现。
    """
    import re as _re
    if not ENV_PATH.exists():
        ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
        ENV_PATH.touch()

    content = ENV_PATH.read_text(encoding="utf-8")
    lines = content.splitlines(keepends=True)
    found = False
    new_lines: list[str] = []
    for line in lines:
        # 加 \b 确保 PPORT 不会误匹配 SPORT 之类的前缀碰撞
        if _re.match(rf"^\s*{_re.escape(key)}\b\s*=", line):
            new_lines.append(f"{key}={value}\n")
            found = True
        else:
            new_lines.append(line)
    if not found:
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines.append("\n")
        new_lines.append(f"{key}={value}\n")
    ENV_PATH.write_text("".join(new_lines), encoding="utf-8")


def resolve_project_path(path_str: str | os.PathLike[str]) -> Path:
    """
    将路径解析为稳定的绝对路径。

    规则
    ----
    - 绝对路径：原样保留
    - 相对路径：统一解释为相对项目根目录（BASE_DIR）

    这样无论在 macOS / Windows、终端 / IDE / 双击脚本下运行，
    `data/...` 和 `.env` 都会落到同一个项目目录，不受当前工作目录影响。
    """
    path = Path(path_str).expanduser()
    return path if path.is_absolute() else (BASE_DIR / path).resolve()


load_dotenv(dotenv_path=ENV_PATH)

# DB_PATH / TIMEZONE 在模块级定义，作为唯一来源。
# load_config() 和 web.py 均从此处引用，不再各自读 os.environ。
# 注意：必须在 load_dotenv() 和 resolve_project_path() 之后定义，
# 确保 .env 已加载、函数已可用。
DB_PATH  = resolve_project_path(os.environ.get("DB_PATH", "data/listings.db"))
TIMEZONE = os.environ.get("TIMEZONE", "Europe/Amsterdam")

BASE_URL = "https://www.holland2stay.com/residences"

# CloakBrowser 设置（用于 H2S CF Turnstile 绕过）
CLOAKBROWSER_HEADLESS = os.environ.get("CLOAKBROWSER_HEADLESS", "true").lower() != "false"
"""True=无头模式（Docker/生产），False=可视化（本地调试）"""

# curl_cffi TLS 指纹模拟池，绕过 Cloudflare WAF。
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
_CURL_IMPERSONATE_POOL = [
    "chrome136",          # Chrome 136 (2025 Q2, 最新)
    "chrome131",          # Chrome 131 (2024 Q4)
    "chrome124",          # Chrome 124 (2024 Q2, fallback)
    "safari18_0",         # Safari 18 (macOS, 2024 秋)
    "safari17_2_ios",     # iOS Safari 17.2（移动端，TLS 与 macOS 不同）
    "firefox135",         # Firefox 135（NSS 栈，与 Chromium 系完全不同）
    "chrome131_android",  # Android Chrome 131（移动端 Chromium）
    "edge101",            # Edge 101 (Windows 默认浏览器)
]
# Chrome 桌面 40% / Safari 25% / Firefox 15% / 移动 15% / Edge 5%
# 接近 NL 桌面浏览器市场实际分布（StatCounter 2025 数据）。
_POOL_WEIGHTS = [4, 4, 2, 3, 2, 3, 1, 1]

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
import time as _time  # noqa: E402  (局部别名，避免与文件其它 time 用法冲突)
import hashlib as _hashlib  # noqa: E402
import re as _re  # noqa: E402
import itertools as _itertools  # noqa: E402
from urllib.parse import (  # noqa: E402
    quote as _quote,
    unquote as _unquote,
    urlparse as _urlparse,
    urlunparse as _urlunparse,
)

_PROXY_COOLDOWN_SEC = 600  # 10 分钟
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


def report_proxy_failure(url: str = "", *, service_error_confirmed: bool = True) -> str:
    """
    记录一次代理故障。``url`` 留空时记录**当前选中**的那个（即刚刚用过、
    刚失败的那个）。

    只有同一代理在确认窗口内连续失败达到阈值，且本次错误已确认是代理
    服务端异常时，才把它放入 cooldown。返回下一轮 get_proxy_url 会用的
    代理；若所有代理都进入冷却则返回空串，表示下一轮将直连服务器原生 IP。
    """
    pool = _proxy_pool()
    target = url.strip() or (pool[0] if pool else "")
    # 标记当前选中的（不是 pool[0]，因为 pool[0] 可能已在冷却）
    if not url:
        target = get_proxy_url()
    if target and _record_proxy_failure_mark(target) and service_error_confirmed:
        _proxy_cooldown_until[target] = _time.monotonic() + _PROXY_COOLDOWN_SEC
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

# 所有已知城市及其 GraphQL filter ID。
# ID 来自 Holland2Stay GraphQL aggregations 接口，city filter 使用字符串形式。
# 新增城市需同时在此处添加，并在 Web 面板城市列表中选择。
KNOWN_CITIES: list[dict] = [
    {"name": "Amersfoort",              "id": "6249"},
    {"name": "Amsterdam",               "id": "24"},
    {"name": "Arnhem",                  "id": "320"},
    {"name": "Capelle aan den IJssel",  "id": "619"},
    {"name": "Delft",                   "id": "26"},
    {"name": "Den Bosch",               "id": "28"},
    {"name": "Diemen",                  "id": "110"},
    {"name": "Dordrecht",               "id": "620"},
    {"name": "Eindhoven",               "id": "29"},
    {"name": "Groningen",               "id": "545"},
    {"name": "Haarlem",                 "id": "616"},
    {"name": "Helmond",                 "id": "6099"},
    {"name": "Leiden",                  "id": "6293"},
    {"name": "Maarssen",                "id": "6209"},
    {"name": "Maastricht",              "id": "6090"},
    {"name": "Nieuwegein",              "id": "6051"},
    {"name": "Nijmegen",                "id": "6217"},
    {"name": "Rijswijk",                "id": "6224"},
    {"name": "Rotterdam",               "id": "25"},
    {"name": "Sittard",                 "id": "6211"},
    {"name": "The Hague",               "id": "90"},
    {"name": "Tilburg",                 "id": "6093"},
    {"name": "Utrecht",                 "id": "27"},
    {"name": "Velp",                    "id": "6265"},
    {"name": "Zeist",                   "id": "6145"},
    {"name": "Zoetermeer",              "id": "6088"},
]

KNOWN_OURCAMPUS_CITIES: list[dict] = [
    {"name": "OurCampus Amsterdam Diemen", "key": "diemen"},
]


KNOWN_OURDOMAIN_CITIES: list[dict] = [
    {"name": "Amsterdam Diemen",    "key": "diemen"},
    {"name": "Amsterdam South-East","key": "south-east"},
]


@dataclass
class CityFilter:
    """GraphQL city filter 的单个城市条目。"""
    name: str   # 显示名，e.g. "Eindhoven"
    id: int     # GraphQL filter 数值 ID，e.g. 29


@dataclass
class AvailabilityFilter:
    """
    GraphQL available_to_book filter 的单个可用性条目。

    已知 ID
    -------
    179 → "Available to book"（可直接预订）
    336 → "Available in lottery"（摇号中）
    """
    label: str  # 可读标签，e.g. "Available to book"
    id: int     # GraphQL filter 数值 ID，e.g. 179


@dataclass
class OurDomainCityFilter:
    """OurDomain / RENTCafe building filter 的单个条目。"""
    name: str
    key: str


@dataclass
class OurCampusCityFilter:
    """OurCampus / RENTCafe building filter 的单个条目。"""
    name: str
    key: str


KNOWN_XIOR_CITIES: list[dict] = [
    {"city": "Aachen Vaals",   "bldg": "Katzensprung",          "key": "p0196061"},
    {"city": "Amsterdam",      "bldg": "Karspeldreef",          "key": "p0196062"},
    {"city": "Amsterdam",      "bldg": "Naritaweg",             "key": "p0196102"},
    {"city": "Breda",          "bldg": "Kraanstraat",           "key": "p0196099"},
    {"city": "Breda",          "bldg": "Rat Verleghstraat",     "key": "p0196103"},
    {"city": "Breda",          "bldg": "Tramsingel 21",         "key": "p0196106"},
    {"city": "Breda",          "bldg": "Tramsingel 27",         "key": "p0196107"},
    {"city": "Delft",          "bldg": "Antonia Veerstraat",    "key": "p0196059"},
    {"city": "Delft",          "bldg": "Barbarasteeg",          "key": "p0196060"},
    {"city": "Delft",          "bldg": "Phoenixstraat",         "key": "p0196499"},
    {"city": "Eindhoven",      "bldg": "Kronehoefstraat",       "key": "p0196467"},
    {"city": "Eindhoven",      "bldg": "Zernikestraat",         "key": "p0195855"},
    {"city": "Groningen",      "bldg": "Eendrachtskade",        "key": "p0196098"},
    {"city": "Groningen",      "bldg": "Oosterhamrikkade",      "key": "p0196468"},
    {"city": "Groningen",      "bldg": "Zernike Tower",         "key": "p0195447"},
    {"city": "Leeuwarden",     "bldg": "Ritsumastraat",         "key": "p0196104"},
    {"city": "Leeuwarden",     "bldg": "Tesselschadestraat",    "key": "p0196105"},
    {"city": "Leiden",         "bldg": "Verbeekstraat",         "key": "p0196501"},
    {"city": "Maastricht",     "bldg": "Annadal",               "key": "p0196111"},
    {"city": "Maastricht",     "bldg": "Bonnefanten",           "key": "p0195680"},
    {"city": "Maastricht",     "bldg": "Vijverdalseweg",        "key": "p0196471"},
    {"city": "Rotterdam",      "bldg": "Burgemeester Oudlaan",  "key": "p0196502"},
    {"city": "The Hague",      "bldg": "Eisenhowerlaan",         "key": "p0196500"},
    {"city": "The Hague",      "bldg": "Lutherse Burgwal",       "key": "p0196100"},
    {"city": "Utrecht",        "bldg": "Rotsoord",              "key": "p0195853"},
    {"city": "Utrecht",        "bldg": "Willem Dreeslaan",      "key": "p0196503"},
    {"city": "Venlo",          "bldg": "Peperstraat",           "key": "p0196469"},
    {"city": "Venlo",          "bldg": "Spoorstraat",           "key": "p0196470"},
    {"city": "Wageningen",     "bldg": "Costerweg",             "key": "p0196465"},
    {"city": "Wageningen",     "bldg": "Duivendaal",            "key": "p0196466"},
]


@dataclass
class XiorCityFilter:
    """Xior / RENTCafe building filter 的单个条目。"""
    name: str
    key: str


# ── 多平台过滤维度能力表 ─────────────────────────────────────────────
#
# 同一套过滤条件作用于三个平台，但各平台 feature_map 能稳定提供的属性不同
# （Xior 没有楼层/户型/片区，RENTCafe 两家都没有 H2S 专有的 contract/tenant/
# offer/finishing/energy）。若直接用「白名单匹配不到就拒绝」会把整批抓不到该
# 属性的房源误杀。
#
# 处理原则：平台**不支持**某维度 → 对该平台跳过该条件（fail-open）；平台支持
# 但本条房源恰好缺值 → 维持原策略（数值 fail-closed / 白名单 no-match 拒绝），
# 不削弱该平台的过滤严格度（对自动预订安全很重要）。
#
# 新增 scraper 时在此登记它能稳定产出的可过滤维度即可。
_UNIVERSAL_FILTER_DIMS = frozenset({"max_rent", "min_area", "city", "source"})
_SOURCE_FILTER_DIMS: dict[str, frozenset] = {
    "holland2stay": _UNIVERSAL_FILTER_DIMS | {
        "floor", "occupancy", "type", "neighborhood",
        "contract", "tenant", "offer", "finishing", "energy",
    },
    "ourdomain": _UNIVERSAL_FILTER_DIMS | {"floor", "occupancy", "type"},
    # OurCampus 复用 OurDomain 的解析器，能提供的维度完全相同
    "ourcampus": _UNIVERSAL_FILTER_DIMS | {"floor", "occupancy", "type"},
    "xior": _UNIVERSAL_FILTER_DIMS,
}


def _source_supports_dim(source: Optional[str], dim: str) -> bool:
    """该平台是否稳定提供某过滤维度。

    未登记的平台默认只认通用维度（price/area/city/source），其余平台专有维度
    一律跳过——多平台场景下宁可放行也别误杀；新平台需要更严的过滤时在
    ``_SOURCE_FILTER_DIMS`` 里登记。
    """
    caps = _SOURCE_FILTER_DIMS.get((source or "holland2stay").strip().lower(),
                                   _UNIVERSAL_FILTER_DIMS)
    return dim in caps


@dataclass
class ListingFilter:
    """
    房源过滤条件。用于决定某条房源是否向用户发送通知，或是否触发自动预订。

    过滤逻辑
    --------
    所有条件之间为 AND 关系：房源必须满足全部已设条件才会放行。
    过滤条件字段为 None / 空列表时，该条件不生效（全部放行）。
    `is_empty()` 返回 True 时整个过滤器不生效。

    fail-closed 原则（数值字段）
    -----------------------------
    max_rent / min_area / min_floor 均采用 fail-closed：
    若过滤条件已设置，但房源对应字段缺失（API 未返回或无法解析），
    则视为不满足条件，返回 False。
    理由：无法核验时放行（fail-open）对自动预订是危险的——
    可能误触发价格未知或面积未知房源的自动预订。

    字符串白名单字段（allowed_occupancy / allowed_types / allowed_neighborhoods）
    本身已是 fail-closed：字段缺失时为空字符串，白名单匹配必然失败。

    注意
    ----
    过滤只影响通知和自动预订触发，不影响数据库写入（所有房源都会入库）。
    面积/楼层数据来自 `Listing.feature_map()`，若 API 返回格式变化可能导致过滤失效。
    """
    max_rent: Optional[float] = None
    """最高月租（€/月）。超出此值的房源不通知。e.g. 1200.0"""

    min_area: Optional[float] = None
    """最小面积（m²）。低于此值的房源不通知。e.g. 20.0"""

    min_floor: Optional[int] = None
    """最低楼层（0=地面层）。低于此楼层的房源不通知。e.g. 1"""

    allowed_occupancy: list[str] = field(default_factory=list)
    """
    入住人数白名单（子串匹配，大小写不敏感）。非空时只通知列表中的类型。
    e.g. ["Single", "Two (only couples)"]
    """

    allowed_types: list[str] = field(default_factory=list)
    """
    房型白名单（子串匹配，大小写不敏感）。非空时只通知列表中的户型。
    e.g. ["Studio", "1", "Loft (open bedroom area)"]
    """

    allowed_neighborhoods: list[str] = field(default_factory=list)
    """
    片区白名单（子串匹配，大小写不敏感）。非空时只通知指定片区的房源。
    e.g. ["Strijp", "Centrum"]
    """

    allowed_cities: list[str] = field(default_factory=list)
    """
    城市白名单（精确匹配城市名，大小写不敏感）。非空时只通知指定城市的房源。
    e.g. ["Eindhoven", "Amsterdam"]
    """

    allowed_sources: list[str] = field(default_factory=list)
    """
    平台白名单（精确匹配 Listing.source，大小写不敏感）。非空时只通知指定平台。
    e.g. ["holland2stay", "ourdomain"]
    """

    allowed_contract: list[str] = field(default_factory=list)
    """
    合同类型白名单（子串匹配，大小写不敏感）。非空时只通知匹配的房源。
    e.g. ["6 months max"] 只推送短租；["Indefinite"] 只推送长租。
    """

    allowed_tenant: list[str] = field(default_factory=list)
    """
    租客要求白名单（子串匹配，大小写不敏感）。非空时只通知匹配的房源。
    e.g. ["student only"] 只推送学生房。
    """

    allowed_offer: list[str] = field(default_factory=list)
    """
    促销/标签白名单（子串匹配，大小写不敏感）。非空时只通知匹配的房源。
    e.g. ["Short-stay"] / ["Parking included"]。
    """

    allowed_finishing: list[str] = field(default_factory=list)
    """
    装修类型白名单（子串匹配，大小写不敏感）。非空时只通知匹配的房源。
    e.g. ["Upholstered"] / ["Shell"]。
    """

    allowed_energy: str = ""
    """
    可接受的最低能耗等级。非空时只通知该等级及以上的房源。
    e.g. "B" → 匹配 A+++/A++/A+/A/B。
    等级排序：A+++ > A++ > A+ > A > B > C > D > E > F...
    """

    def __post_init__(self) -> None:
        """把同义的合同取值归一后再落库。

        匹配那一侧已经两边都归一了（见 ``passes``），所以就算存的是荷兰语
        原文也能匹配上。但 ``/api/v1/filter/options`` 现在只返回归一后的
        ``Indefinite``——如果用户存的还是 ``Onbepaalde tijd``，iOS 端拿
        options 渲染勾选框时就找不到这一项：界面上 Indefinite 没打勾，
        实际过滤却生效着，成了一个看不见的选中项。

        在这里统一（而不是在 API 和表单各写一遍）：所有构造路径——API PUT、
        Web 表单、从 DB 读回——都要经过 ``__init__``，存量数据下次保存时
        自动收敛。
        """
        self.allowed_contract = list(dict.fromkeys(
            canonical_feature(v) for v in (self.allowed_contract or [])
        ))

    def is_empty(self) -> bool:
        """所有条件均未设置时返回 True，表示全部放行。"""
        # 通过遍历 dataclass fields 自动判断，新增过滤字段无需手动同步此处
        for f in fields(self):
            if f.name == "allowed_energy":
                if isinstance(self.allowed_energy, str) and self.allowed_energy.strip():
                    return False
            elif isinstance(getattr(self, f.name), list):
                if getattr(self, f.name):
                    return False
            elif getattr(self, f.name) is not None:
                return False
        return True

    def passes(self, listing: "Listing") -> bool:
        """
        判断房源是否通过过滤条件。

        Parameters
        ----------
        listing : Listing
            待判断的房源快照

        Returns
        -------
        True  → 满足所有过滤条件，应发送通知
        False → 不满足至少一项条件，跳过
        """
        fm = listing.feature_map()

        # 数值过滤采用 fail-closed 原则：
        # 过滤条件已设置但字段缺失（无法核验）时，视为不满足条件，返回 False。
        # 这对自动预订尤为重要——不能因数据缺失而误触发高价/不合适房源的预订。
        #
        # 拒绝原因细分（便于用户排查）：
        #   字段缺失 → WARNING（API 未返回该字段，但过滤条件已设置）
        #   值不符   → 静默返回 False（正常过滤，无需提示）

        if self.max_rent is not None:
            price = listing.price_value
            if price is None:
                logger.warning(
                    "过滤拒绝 [%s]: 已设 max_rent=%.0f 但价格字段缺失（API 未返回）",
                    listing.name, self.max_rent,
                )
                return False
            if price > self.max_rent:
                return False

        area_str = fm.get("area", "")
        area = parse_float(area_str)
        if self.min_area is not None:
            if area is None:
                logger.warning(
                    "过滤拒绝 [%s]: 已设 min_area=%.0f 但面积字段缺失（API 未返回）",
                    listing.name, self.min_area,
                )
                return False
            if area < self.min_area:
                return False
        # min_floor：floor 是平台相关维度（Xior 不返回楼层）——平台不支持时跳过，
        # 支持的平台仍 fail-closed（缺值即拒绝，保自动预订安全）。
        if self.min_floor is not None and _source_supports_dim(listing.source, "floor"):
            floor_str = fm.get("floor", "")
            floor = parse_int(floor_str)
            if floor is None:
                logger.warning(
                    "过滤拒绝 [%s]: 已设 min_floor=%d 但楼层字段缺失（API 返回: %r）",
                    listing.name, self.min_floor, floor_str,
                )
                return False
            if floor < self.min_floor:
                return False

        # ── 平台相关白名单维度 ─────────────────────────────────────────
        # 这些维度并非每个平台都有（见 _SOURCE_FILTER_DIMS）。平台**不支持**该
        # 维度 → 跳过该条件（fail-open，避免一套过滤条件误杀整批房源）；平台
        # 支持但本条缺值 → 白名单匹配失败照常拒绝（不削弱该平台过滤严格度）。
        if self.allowed_occupancy and _source_supports_dim(listing.source, "occupancy"):
            occ = fm.get("occupancy", "")
            if not any(a.lower() in occ.lower() for a in self.allowed_occupancy):
                return False

        if self.allowed_types and _source_supports_dim(listing.source, "type"):
            rtype = fm.get("type", "")
            if not any(a.lower() in rtype.lower() for a in self.allowed_types):
                return False

        if self.allowed_neighborhoods and _source_supports_dim(listing.source, "neighborhood"):
            nbhd = fm.get("neighborhood", "")
            if not any(a.lower() in nbhd.lower() for a in self.allowed_neighborhoods):
                return False

        if self.allowed_cities:
            city = listing.city or ""
            if not any(a.lower() == city.lower() for a in self.allowed_cities):
                return False

        if self.allowed_sources:
            source = listing.source or "holland2stay"
            if not any(a.lower() == source.lower() for a in self.allowed_sources):
                return False

        # ── H2S 专有维度（contract / tenant / offer / finishing / energy）──
        # 只有 H2S 稳定返回；其它平台不支持 → _source_supports_dim 返回 False
        # 而整体跳过。
        if self.allowed_contract and _source_supports_dim(listing.source, "contract"):
            # 两边都先归一：房源上写的可能是 "Onbepaalde tijd"，而下拉里只剩
            # 归一后的 "Indefinite"（老用户存下来的值也可能是荷兰语原文）。
            # 不归一就会出现「勾了 Indefinite 却收不到 38 条同义房源」。
            contract = canonical_feature(fm.get("contract", ""))
            if not any(canonical_feature(a).lower() in contract.lower()
                       for a in self.allowed_contract):
                return False

        if self.allowed_tenant and _source_supports_dim(listing.source, "tenant"):
            tenant = fm.get("tenant", "")
            if not any(a.lower() in tenant.lower() for a in self.allowed_tenant):
                return False

        if self.allowed_offer and _source_supports_dim(listing.source, "offer"):
            offer = fm.get("offer", "")
            if not any(a.lower() in offer.lower() for a in self.allowed_offer):
                return False

        if self.allowed_finishing and _source_supports_dim(listing.source, "finishing"):
            furnishing = fm.get("furnishing", "")
            if not any(a.lower() in furnishing.lower() for a in self.allowed_finishing):
                return False

        if (isinstance(self.allowed_energy, str) and self.allowed_energy.strip()
                and _source_supports_dim(listing.source, "energy")):
            min_rank = energy_rank(self.allowed_energy)
            if min_rank is None:
                logger.warning("无效能耗等级配置 %r，过滤条件忽略", self.allowed_energy)
                return False  # 配置了无效等级（如 "banana"）→ fail-closed
            energy = fm.get("energy_label", "").strip().upper()
            actual_rank = energy_rank(energy)
            if actual_rank is None:
                logger.warning("房源 %r 能耗标签不在白名单中: %r", listing.name, energy)
                return False
            if actual_rank > min_rank:
                return False

        return True


#: 申请人档案里**加密存储**的字段。
#:
#: 只加密真正抬高身份盗用风险的两项。其余（姓名、电话、大学）与库里既有的
#: email / telegram_chat_id 同级，保持明文——全都加密会让这张表和其它表的
#: 处理方式不一致，反而容易在某处漏掉。
_ENCRYPTED_PROFILE_FIELDS = ("date_of_birth", "address", "id_number")

#: RENTCafe 申请表的下拉选项，抄自实测页面（Vaals / Katzensprung）。
#: 值必须与页面 option 文本完全一致，否则提交时匹配不上。
APPLICANT_TITLES = ("Mr.", "Ms.", "Mrs.", "Dr.")
APPLICANT_GENDERS = ("Male", "Female", "Gender Nonbinary", "Prefer Not to Disclose")


@dataclass
class ApplicantProfile:
    """RENTCafe 申请表（Applicant Info）里的个人资料。

    证件扫描件**不在这里**，但系统确实要存——见 :mod:`applicant_docs`。
    平台在 `ID/Passport Upload` 到位前拒绝保存申请表的任何内容，而自动预订是
    异步触发的，所以不存在「用完即走的透传」。这是一个知情的取舍（当前部署
    只服务少量熟人，自动预订未对外开放）；文件单独加密落盘，不进这个每轮都要
    加载的配置对象。

    **付款始终不代做。** ``ApplicationCharges`` 那一步要填 IBAN / SWIFT /
    户名（2026-08-03 侦察确认），代填金融凭据是硬限制。

    字段与实测表单一一对应：

    ==================  ================================================
    表单字段             说明
    ==================  ================================================
    Title               见 :data:`APPLICANT_TITLES`
    First/Middle/Last   中间名可用 ``no_middle_name`` 勾选「我没有中间名」
    Phone / Gender      Gender 见 :data:`APPLICANT_GENDERS`
    Date Of Birth       Screening 区块，`YYYY-MM-DD` 存储，提交时转 `d-m-yyyy`
    Nationality         国家名（存显示名而非页面的数字 id——id 会随上游改版
                        失效，显示名更稳，提交时再匹配）
    Country / Address / Post Code-City
    University          Xior 特有的必填项（学生住房）
    min_lease_term      最短租期（月）
    ==================  ================================================
    """

    title: str = ""
    first_name: str = ""
    middle_name: str = ""
    no_middle_name: bool = False
    last_name: str = ""
    phone: str = ""
    gender: str = ""
    date_of_birth: str = ""      # YYYY-MM-DD
    nationality: str = ""
    country: str = "Netherlands"
    address: str = ""
    postcode_city: str = ""
    university: str = ""
    min_lease_term: str = ""
    #: Screening 区块的两个必填项（实测在页面底部的 Information 小节）。
    place_of_birth: str = ""
    #: 护照号 / 身份证号。**加密存储**——它和姓名/生日/地址/国籍凑在一起
    #: 就是一份完整身份信息包，泄露后果与证件扫描件同级。
    id_number: str = ""
    #: 学号，非必填（Xior 是学生住房，填了有助于审核）。
    student_number: str = ""

    # ------------------------------------------------------------------
    # 2026-08-03 对着真实表单补的字段。上一版把 15 个字段名全写错了（命中 0），
    # 修正时才发现表单要的东西比档案里存的多。详见 bookers/rentcafe_form.py。
    # ------------------------------------------------------------------
    #: 地址第二行（门牌补充）。表单把地址拆成 Addr1/Addr2/ZipCode/City 四格，
    #: 原来的 ``postcode_city`` 一格塞两样，对不上。
    address_line2: str = ""
    #: 邮编。留空时从旧的 ``postcode_city`` 里拆（见 :meth:`_split_postcode_city`）。
    postcode: str = ""
    #: 城市。同上。
    city: str = ""
    #: 证件签发国（表单 ``drpDLCountry``，必填）。
    id_country: str = ""
    #: 当前住所性质：``Rent`` / ``Own`` / ``Other``（表单 ``OwnerShipType``）。
    housing_type: str = ""

    # -- 背景调查三问 ---------------------------------------------------
    # 取值 ``Yes`` / ``No``，**留空表示用户还没回答**。
    #
    # 这三项和其它字段有本质区别：它们是关于用户本人的**事实陈述**（是否曾被
    # 驱逐、是否曾被定罪、是否有未决刑事指控）。系统绝不能默认填 "No"——代勾
    # 「我授权你做背景调查」是用户授权过的，代答「我没有前科」不是；答错了是
    # 用户在承担后果。没回答就不提交，让服务端拒绝。
    ever_evicted: str = ""
    ever_convicted: str = ""
    criminal_charges: str = ""

    def _split_postcode_city(self) -> tuple[str, str]:
        """兼容旧档案里 ``postcode_city`` 一格塞两样的写法。

        荷兰邮编形如 ``6291 AB``，后面跟城市名。新字段有值时一律以新字段为准。
        """
        if self.postcode or self.city:
            return self.postcode.strip(), self.city.strip()
        raw = (self.postcode_city or "").strip()
        # 荷兰邮编是 4 位数字 + 2 个字母，城市名跟在后面（可以没有）。
        # 城市部分为空时**不要**把整串当城市——`5652EN` 是邮编不是城市，
        # 贴错标签不会报错，只会往申请表的 City 格里填一串邮编。
        m = re.match(r"^(\d{4}\s*[A-Za-z]{2})\b\s*(.*)$", raw)
        if m:
            return m.group(1).strip(), m.group(2).strip()
        return "", raw

    def is_complete(self) -> bool:
        """是否够填完 Applicant Info 的全部必填项。

        不完整时不该触发半自动预订——填一半的表单提交不上去，只会白白消耗
        RENTCafe 的尝试额度，还在用户账号下留一条废弃申请。
        """
        return not self.missing_fields()

    def missing_fields(self) -> list[str]:
        """还缺哪些必填项，给面板提示用。

        这张清单对着 2026-08-03 的真实表单核过一遍。修正字段名时发现表单要的
        东西比档案里存的多（证件签发国、住所性质、背景调查三问），漏填的后果
        不是报错——服务端会静默丢弃不认识的字段，提交一份缺项的申请。
        """
        postcode, city = self._split_postcode_city()
        checks = {
            "first_name": self.first_name, "last_name": self.last_name,
            "gender": self.gender, "date_of_birth": self.date_of_birth,
            "country": self.country, "address": self.address,
            "postcode": postcode, "city": city,
            "place_of_birth": self.place_of_birth, "id_number": self.id_number,
            "housing_type": self.housing_type,
            # 表单上标着「Nationality」的那一格，字段名其实是 drpDLCountry
            # （见 bookers/rentcafe_applicant._dl_country_attr）
            "nationality": self.nationality,
            # 背景调查三问：留空 = 用户还没回答。系统不替他答，所以这里必须
            # 当成"缺必填项"，而不是当成"答 No"。
            "ever_evicted": self.ever_evicted,
            "ever_convicted": self.ever_convicted,
            "criminal_charges": self.criminal_charges,
        }
        out = [k for k, v in checks.items() if not str(v).strip()]
        # 中间名要么填了，要么显式勾了「没有」——留空且没勾，表单过不了校验
        if not str(self.middle_name).strip() and not self.no_middle_name:
            out.append("middle_name")
        return out


@dataclass
class AutoBookConfig:
    """
    单个用户的自动预订配置。

    字段说明
    --------
    enabled         : 总开关。False 时整个自动预订跳过，不登录也不调用任何 API
    dry_run         : 试运行模式。True 时只做登录/购物车验证，不执行 addNewBooking；
                      默认 True，需显式设为 False 才真正提交预订
    email           : Holland2Stay 账号邮箱
    password        : Holland2Stay 账号密码（加密后存储于 SQLite user_configs）
    listing_filter  : 独立于通知过滤的预订条件，可以设置比通知更严格的门槛；
                      is_empty() 为 True 时对所有 Available to book 房源都会触发
    cancel_enabled  : 是否启用自动取消旧订单功能。False 时 placeOrder 返回
                      "another unit reserved" 会直接通知用户（不尝试取消），
                      因为 H2S 平台的 cancelOrder mutation 默认未启用
    payment_method  : setPaymentMethodOnCart 使用的支付方式代码。
                      可选值（均来自浏览器抓包）：
                        "idealcheckout_ideal"       → iDEAL（荷兰网银，推荐）
                        "idealcheckout_visa"        → Visa 信用卡
                        "idealcheckout_mastercard"  → Mastercard 信用卡
                      注意：Visa / Mastercard 仅适用于已在 H2S 账号绑定对应卡的用户。

    平台凭据独立
    ------------
    H2S、Xior、OurDomain 是三个不同平台、不同账号（Xior 与 OurDomain 还是
    不同的 RENTCafe 租户，账号不互通），各自一套凭据：

    - H2S：``email`` / ``password`` / ``payment_method``
    - Xior：``xior_accounts``（**按楼栋**，见下）
    - OurDomain：``ourdomain_email`` / ``ourdomain_password``

    RENTCafe 平台只存账号凭据——用户须自行在浏览器注册账号，个人信息
    （姓名/电话/出生日期）注册时已录入 RENTCafe，booker 登录后无需再填。

    Xior 是**一栋楼一个账号**
    -------------------------
    2026-08-03 实测：Xior 的每栋楼是一个独立的 RENTCafe property 门户，各有
    自己的 host、property 代码和 ``myOlePropertyId``；登录页原话是「your
    <楼栋名> Guest Account」。cookie 不跨主机，账号也不互通。

    ======================  ==========================================  ========
    楼栋                     host                                        属性代码
    ======================  ==========================================  ========
    Eindhoven Zernikestraat  zernikestraat-xiorstudenthousing…           NLEZERNS
    Aachen Vaals             sneeuwberglaan-xiorstudenthousing…          NLVSNEES
    Utrecht Willem Dreeslaan willemdreeslaan-xiorstudenthousing…         NLUWIDRS
    ======================  ==========================================  ========

    所以 Xior 凭据按楼栋 key 存：``{building_key: {"email": …, "password": …}}``，
    key 与 ``XIOR_CITIES`` / ``XiorScraper.BUILDINGS`` 用的是同一套（如
    ``p0196062``）。**没有该楼凭据 = 该楼不参与自动预订**，凭据本身就是开关，
    不需要额外的 per-building 开关。

    迁移：旧版共用一套 ``email``/``password``，``users._ab_from_dict`` 在加载时
    把旧值回填进各平台字段；更早的单对 ``xior_email``/``xior_password`` 保留
    为只读兼容字段，见 ``xior_account_for()``。
    """
    enabled: bool = False
    dry_run: bool = True
    listing_filter: ListingFilter = field(default_factory=ListingFilter)
    cancel_enabled: bool = False

    # ── H2S ──
    email: str = ""
    password: str = ""
    payment_method: str = "idealcheckout_ideal"

    # ── Xior（RENTCafe 租户 xiorstudenthousing，一栋楼一个账号）──
    #: ``{building_key: {"email": str, "password": str}}``
    xior_accounts: dict[str, dict[str, str]] = field(default_factory=dict)

    #: RENTCafe 申请表的个人资料。半自动预订用它自动填 Applicant Info，
    #: 用户只需自己上传证件 + 付款——那两步系统不该代劳。
    applicant_profile: ApplicantProfile = field(default_factory=ApplicantProfile)

    #: 用户预先授权系统代勾申请表上那两个法律声明的时间（ISO，UTC）。空 = 未授权。
    #:
    #: 那两句是「我授权做信用/背景调查」和「我确认所填属实」。系统替人勾这种
    #: 声明和替人填地址不是一回事，所以：
    #:
    #: 1. 必须由用户在面板上显式授权一次；
    #: 2. **存时间戳而不是布尔值**——将来若有争议，要能说清是哪一刻授权的。
    #:    布尔值只能回答「有没有」，回答不了「什么时候」。
    #: 3. 没有它，booker 不会提交（见 XiorBooker.book 的前置校验）。
    screening_consent_at: str = ""

    def has_screening_consent(self) -> bool:
        return bool((self.screening_consent_at or "").strip())

    # 旧的单对字段。保留只为兼容存量配置——**新代码不要读它**，
    # 走 xior_account_for()，那里会处理回退。
    xior_email: str = ""
    xior_password: str = ""

    # ── OurDomain（RENTCafe 租户 thisisourdomain）──
    ourdomain_email: str = ""
    ourdomain_password: str = ""

    def xior_account_for(self, building_key: str) -> tuple[str, str]:
        """取某栋楼的 Xior 账号，返回 ``(email, password)``；没有则返回 ``("", "")``。

        找不到时**不回退到任何其他楼的凭据**——拿 A 楼的账号去 B 楼的门户登录
        只会失败，而且失败会计入 RENTCafe 的 IP 级尝试限制（连续失败锁 30 分钟），
        等于用一次注定失败的请求去消耗真正需要它的额度。

        存量的单对 ``xior_email``/``xior_password`` 只在**用户还没配过任何按楼
        凭据**时兜底：那批配置是在「Xior 一个账号」的错误认知下填的，只可能对
        其中某一栋楼有效，无法判断是哪栋，所以一旦用户开始按楼配置就彻底忽略它。
        """
        key = (building_key or "").strip()
        acct = (self.xior_accounts or {}).get(key)
        if acct:
            return acct.get("email", ""), acct.get("password", "")
        if not self.xior_accounts and self.xior_email:
            return self.xior_email, self.xior_password
        return "", ""

    def xior_buildings(self) -> list[str]:
        """已配置凭据的楼栋 key 列表（email 和 password 都非空才算）。"""
        return sorted(
            k for k, v in (self.xior_accounts or {}).items()
            if (v or {}).get("email") and (v or {}).get("password")
        )


@dataclass
class Config:
    """
    全局运行配置，从 .env 加载，影响整个监控进程。

    字段说明
    --------
    check_interval      : 常规轮询间隔（秒），对应 .env CHECK_INTERVAL
    cities              : 要监控的城市列表，对应 .env CITIES（格式 "城市名,ID|..."）
    availability_filters: GraphQL available_to_book filter 列表，
                          对应 .env AVAILABILITY_FILTERS（格式 "标签,ID|..."）
    db_path             : SQLite 数据库文件路径，对应 .env DB_PATH
    log_level           : 日志级别字符串，对应 .env LOG_LEVEL

    智能轮询（荷兰高峰期加速）
    --------------------------
    peak_interval       : 高峰期轮询间隔初始值（秒），对应 .env PEAK_INTERVAL；
                          也是自适应轮询的起点，被限流后会在此值上翻倍退避
    peak_start          : 第一个高峰开始时间（荷兰本地时间 HH:MM），对应 .env PEAK_START
    peak_end            : 第一个高峰结束时间（荷兰本地时间 HH:MM），对应 .env PEAK_END
    peak_start_2        : 第二个高峰开始时间（荷兰本地时间 HH:MM），对应 .env PEAK_START_2
    peak_end_2          : 第二个高峰结束时间（荷兰本地时间 HH:MM），对应 .env PEAK_END_2
    peak_weekdays_only  : True 表示仅工作日启用高峰轮询，对应 .env PEAK_WEEKDAYS_ONLY
    min_interval        : 自适应轮询的下限（秒），对应 .env MIN_INTERVAL；
                          高峰期连续成功时间隔会逐步压低，但不会低于此值；
                          建议 ≥ 15s，过低容易触发 429
    jitter_ratio        : 轮询间隔随机抖动比例（0–0.5），对应 .env JITTER_RATIO；
                          e.g. 0.20 表示实际等待时间在基准值 ±20% 范围内随机浮动，
                          避免多实例在同一时刻集中发起请求
    timezone            : IANA 时区标识符，用于图表日期分组和智能轮询时段判定，
                          对应 .env TIMEZONE；默认 Europe/Amsterdam（荷兰时间 CET/CEST）
    heartbeat_interval_minutes : 心跳通知间隔（分钟），对应 .env HEARTBEAT_INTERVAL_MINUTES；
                                 默认 60 分钟；设为 0 禁用心跳
    """
    check_interval: int
    cities: list[CityFilter]
    availability_filters: list[AvailabilityFilter]
    db_path: Path
    log_level: str
    peak_interval: int = 60
    peak_start: str = "08:30"
    peak_end: str = "10:00"
    peak_start_2: str = "13:30"
    peak_end_2: str = "15:00"
    peak_weekdays_only: bool = True
    min_interval: int = 15
    jitter_ratio: float = 0.20
    timezone: str = "Europe/Amsterdam"
    heartbeat_interval_minutes: int = 60
    sources: list[str] = field(default_factory=lambda: ["holland2stay"])
    # 影子 source：照常抓取入库，但**不发任何通知**（用户渠道 + 面板 feed + 推送）。
    # 用于新平台上线前的静默验证——先确认它抓得对、数据长什么样，再决定是否
    # 对用户开放。必须是 sources 的子集才有意义（不在 sources 里就压根不会抓）。
    shadow_sources: list[str] = field(default_factory=list)
    # 分轮抓取：``{source: 每轮最多抓几个 target}``。target 数超过它时，本轮只抓
    # 一个切片，游标持久化在 meta 里逐轮轮转，若干轮覆盖一遍全部。
    #
    # 为什么需要：Xior 的请求间隔是 5s（限流按速率算，见 XIOR.md §2.2），实测
    # **每栋楼 13.9 秒**。30 栋 ≈ 417 秒/轮，而 CHECK_INTERVAL 才 300 秒；更糟的是
    # H2S 排在其它 source 之后执行，等于每轮把真正出房源的那个 source 推迟 7 分钟。
    #
    # 正确解法是分轮抓，不是把请求间隔调小——间隔调小会直接撞回 429。
    shard_sizes: dict[str, int] = field(default_factory=dict)
    #: ``{source: 最小间隔秒数}``。控制「多久抓一次」，和分片的
    #: 「每轮抓几个」是两回事——见 _DEFAULT_SOURCE_MIN_INTERVALS。
    source_min_intervals: dict[str, int] = field(default_factory=dict)
    ourdomain_cities: list[OurDomainCityFilter] = field(default_factory=list)
    ourcampus_cities: list[OurCampusCityFilter] = field(default_factory=list)
    xior_cities: list[XiorCityFilter] = field(default_factory=list)

    def scrape_tasks_v2(self) -> list["ScrapeTask"]:  # type: ignore[name-defined]
        """
        P0 新接口：展开为 source-aware 的 ``ScrapeTask`` 列表。

        按 ``SOURCES`` env 变量 + 各 source 自己的 ``{NAME}_CITIES`` 配置
        展开成多 source 的混合列表。

        每个 H2S task 把 ``availability_ids`` 塞进 ``extra``，让
        ``HollandStayScraper.scrape()`` 能拿到 H2S 专有的可用性过滤参数；
        其他 source 不需要这个字段就忽略。

        Returns
        -------
        list[ScrapeTask]
        """
        # 延迟 import 避免 config 加载链路提前触发 scrapers 包初始化
        from scrapers.base import ScrapeTask  # noqa: WPS433

        tasks: list[ScrapeTask] = []
        availability_ids = [str(af.id) for af in self.availability_filters]

        if "holland2stay" in self.sources:
            tasks.extend(
                ScrapeTask(
                    source="holland2stay",
                    city_key=str(c.id),
                    city_display=c.name,
                    extra={"availability_ids": list(availability_ids)},
                )
                for c in self.cities
            )

        if "ourdomain" in self.sources:
            tasks.extend(
                ScrapeTask(
                    source="ourdomain",
                    city_key=c.key,
                    city_display=c.name,
                )
                for c in self.ourdomain_cities
            )

        if "ourcampus" in self.sources:
            tasks.extend(
                ScrapeTask(
                    source="ourcampus",
                    city_key=c.key,
                    city_display=c.name,
                )
                for c in self.ourcampus_cities
            )

        if "xior" in self.sources:
            tasks.extend(
                ScrapeTask(
                    source="xior",
                    city_key=c.key,
                    city_display=c.name,
                )
                for c in self.xior_cities
            )

        return tasks


#: ``SHARD_SIZES`` 未配置时的默认分片大小。
#:
#: 只给 xior 设默认值，因为只有它的单 target 成本高到会顶破轮次预算（实测
#: 13.9s/栋，其余三个 source 都在 1–4s）。4 栋 ≈ 56 秒。
#:
#: 值 ≥ 实际 target 数时分片自动失效。
#:
#: **它管的是「每轮抓几个」，不管「多久抓一次」。** 后者是
#: :data:`_DEFAULT_SOURCE_MIN_INTERVALS`，两者要配合使用——2026-08-04 实测：
#: 楼栋数缩到 4 栋后光靠分片，等于每轮全抓，而高峰时段轮次间隔只有 60–90 秒，
#: 单栋楼的请求频率照样是 30 栋时期的 10 倍，一样撞 429。
_DEFAULT_SHARD_SIZES: dict[str, int] = {"xior": 4}


#: ``SOURCE_MIN_INTERVALS`` 未配置时的默认值（秒）。
#:
#: **分片管的是「每轮抓几个 target」，这个管的是「多久抓一次」——两者解决的
#: 不是同一个问题。** 2026-08-04 生产实测把两者混为一谈的后果：
#:
#: Xior 从 30 栋缩到 4 栋后，分片 3/轮 意味着几乎每栋楼每轮都被抓；而高峰时段
#: 轮次间隔只有 60–90 秒（``MIN_INTERVAL=20`` + PEAK 窗口），于是**单栋楼的
#: 请求频率从「每 10–15 分钟一次」涨到「每 60–90 秒一次」，约 10 倍**，直接
#: 撞进限流：持续 429、退避 30s+60s，单轮从 40 秒拖到 270 秒。
#:
#: 楼栋数变少反而更容易被限流，是因为限流按「同一个 target 被打的频率」算，
#: 而不是按总请求量——30 栋轮着抓时每栋自然稀疏，4 栋轮着抓就全挤在一起了。
#:
#: 值怎么定的：可持续频率的两端都实测过，中间没有。
#:
#: ===========  ==========  ========  =========
#: 每栋楼频率     均耗时       峰值       429
#: ===========  ==========  ========  =========
#: 60–90 秒      64.4s       274s      6/122 轮
#: 3 分钟        待测        —         —
#: 5 分钟        59.7s       65s       0/7 轮
#: 10 分钟       59.4s       62s       0/3 轮
#: ===========  ==========  ========  =========
#:
#: 注意被限流的代价不是「慢一点」，而是那一轮退避 90 秒起、最长 274 秒——
#: **抓得越勤，实际拿到数据反而越晚**，和目的相反。
#:
#: 所以真实可持续值在 90–600 秒之间，正在自上而下逼近：600 → 300 都干净，
#: 现在试 180（每栋楼约 4 分钟一次，仍比出问题时松 3 倍）。若 180 也干净，
#: 下一档再往 120 试。**别直接设 0**：那就是回到上表第一行的状态。
_DEFAULT_SOURCE_MIN_INTERVALS: dict[str, int] = {"xior": 180}


def _parse_source_min_intervals(raw: str) -> dict[str, int]:
    """解析 ``SOURCE_MIN_INTERVALS``，形如 ``xior:600,ourdomain:120``（秒）。

    留空用 :data:`_DEFAULT_SOURCE_MIN_INTERVALS`；显式写 ``xior:0`` 关掉该
    source 的节流。非法条目忽略而不是报错——配置写错不该让监控起不来。
    """
    out = dict(_DEFAULT_SOURCE_MIN_INTERVALS)
    if not (raw or "").strip():
        return out
    for part in raw.split(","):
        if ":" not in part:
            continue
        name, _, secs = part.partition(":")
        name = name.strip().lower()
        try:
            n = int(secs.strip())
        except (TypeError, ValueError):
            continue
        if name and n >= 0:
            out[name] = n
    return out


def _parse_shard_sizes(raw: str) -> dict[str, int]:
    """解析 ``SHARD_SIZES``，形如 ``xior:5,ourdomain:3``。

    留空用 :data:`_DEFAULT_SHARD_SIZES`；显式写 ``xior:0`` 可以关掉某个 source
    的分片（0 表示不限制）。非法条目忽略而不是报错——配置写错不该让监控起不来。
    """
    if not (raw or "").strip():
        return dict(_DEFAULT_SHARD_SIZES)
    out = dict(_DEFAULT_SHARD_SIZES)
    for part in raw.split(","):
        if ":" not in part:
            continue
        name, _, size = part.partition(":")
        name = name.strip().lower()
        try:
            n = int(size.strip())
        except (TypeError, ValueError):
            continue
        if name and n >= 0:
            out[name] = n
    return out


#: 系统支持的全部平台。**新增平台只改这里。**
#:
#: 这个常量是补出来的：原先每处各自维护一份平台清单，于是 ``ourcampus``
#: 被漏了三次——前端的 sourceLabel（三份实现都没有它）、monitoring 页（干脆
#: 不转换）、以及全局设置的白名单（从面板保存一次就会把它从 SOURCES 里悄悄
#: 删掉）。漏的都是同一个平台，因为它是最后加的那个。
KNOWN_SOURCES: tuple[str, ...] = ("holland2stay", "ourdomain", "ourcampus", "xior")

#: 平台显示名。前端另有一份同样的表（static/app.js 的 SOURCE_LABELS），
#: 因为那边是客户端渲染；两边都从这份注释里的同一个事实来。
SOURCE_DISPLAY_NAMES: dict[str, str] = {
    "holland2stay": "Holland2Stay",
    "ourdomain": "OurDomain",
    "ourcampus": "OurCampus",
    "xior": "Xior",
}


def source_display_name(source: str) -> str:
    """平台显示名；未知 key 首字母大写后返回，不套用某个已知平台名。"""
    key = (source or "").strip().lower()
    return SOURCE_DISPLAY_NAMES.get(key, key.capitalize() if key else "")


def _parse_sources_raw(raw: str) -> list[str]:
    """拆 source 列表，不做任何默认值填充。"""
    values = [
        p.strip().lower()
        for p in re.split(r"[,|]", raw or "")
        if p.strip()
    ]
    return list(dict.fromkeys(values))


def _parse_sources(raw: str) -> list[str]:
    return _parse_sources_raw(raw) or ["holland2stay"]


def _parse_name_key_list(raw: str, cls: type):
    """Parse ``name,key|name,key`` into a list of *cls* instances."""
    items = []
    for entry in (raw or "").split("|"):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.rsplit(",", 1)
        if len(parts) == 2:
            items.append(cls(name=parts[0].strip(), key=parts[1].strip()))
    return items


def _parse_ourdomain_cities(raw: str) -> list[OurDomainCityFilter]:
    return _parse_name_key_list(raw, OurDomainCityFilter)


def _parse_ourcampus_cities(raw: str) -> list[OurCampusCityFilter]:
    return _parse_name_key_list(raw, OurCampusCityFilter)


def _parse_xior_cities(raw: str) -> list[XiorCityFilter]:
    return _parse_name_key_list(raw, XiorCityFilter)


def load_config() -> Config:
    """
    从环境变量（已由 dotenv 加载）构造并返回 Config 实例。

    读取的 .env 键
    --------------
    CHECK_INTERVAL          int，默认 300
    SOURCES                 逗号或 | 分隔，默认 "holland2stay"
    CITIES                  格式 "城市名,ID|城市名,ID"，默认 "Eindhoven,29"
    OURDOMAIN_CITIES        格式 "显示名,key|显示名,key"，启用 ourdomain 时默认 Amsterdam Diemen
    OURCAMPUS_CITIES        同上格式，启用 ourcampus 时默认 OurCampus Amsterdam Diemen
    SHADOW_SOURCES          逗号分隔。列出的 source 照常抓取入库但**不发通知**，
                            用于新平台的静默验证。必须是 SOURCES 的子集
    AVAILABILITY_FILTERS    格式 "标签,ID|标签,ID"，默认包含 179 和 336
    DB_PATH                 str，默认 "data/listings.db"
    LOG_LEVEL               str，默认 "INFO"
    PEAK_INTERVAL           int，默认 60
    PEAK_START              str HH:MM，默认 "08:30"
    PEAK_END                str HH:MM，默认 "10:00"
    PEAK_START_2            str HH:MM，默认 "13:30"
    PEAK_END_2              str HH:MM，默认 "15:00"
    PEAK_WEEKDAYS_ONLY      "true"/"false"，默认 "true"
    MIN_INTERVAL            int ≥ 5，默认 "15"（自适应下限，不低于此值）
    JITTER_RATIO            float 0–0.5，默认 "0.20"
    TIMEZONE                IANA 时区，默认 "Europe/Amsterdam"（荷兰 CET/CEST）
    HEARTBEAT_INTERVAL_MINUTES int，默认 60；设为 0 禁用心跳

    Raises
    ------
    ValueError  若 CITIES 或 AVAILABILITY_FILTERS 中的 ID 不是合法整数
    ValueError  若 TIMEZONE 不是合法的 IANA 时区标识符
    """
    interval = int(os.environ.get("CHECK_INTERVAL") or "300")
    sources = _parse_sources(os.environ.get("SOURCES", "holland2stay"))

    cities: list[CityFilter] = []
    raw_cities = os.environ.get("CITIES", "Eindhoven,29")
    for entry in raw_cities.split("|"):
        parts = entry.strip().rsplit(",", 1)
        if len(parts) == 2:
            cities.append(CityFilter(name=parts[0].strip(), id=int(parts[1].strip())))

    availability_filters: list[AvailabilityFilter] = []
    raw_filters = os.environ.get(
        "AVAILABILITY_FILTERS", "Available to book,179|Available in lottery,336"
    )
    for entry in raw_filters.split("|"):
        parts = entry.strip().rsplit(",", 1)
        if len(parts) == 2:
            availability_filters.append(
                AvailabilityFilter(label=parts[0].strip(), id=int(parts[1].strip()))
            )

    ourdomain_cities: list[OurDomainCityFilter] = []
    if "ourdomain" in sources:
        raw_od_cities = os.environ.get("OURDOMAIN_CITIES", "Amsterdam Diemen,diemen")
        ourdomain_cities = _parse_ourdomain_cities(raw_od_cities)

    # 影子 source：抓但不通知。只保留同时也在 sources 里的，否则是配置笔误。
    #
    # 但"静默丢弃"会造出一个很难看穿的假象：SHADOW_SOURCES 里列着 ourcampus，
    # 而 SOURCES 里没有它——看配置像是"影子模式跑着"，实际上它压根没被抓，
    # 数据健康面板上自然也没有它的卡片。2026-08-04 线上就是这个状态，
    # 而且是从设置面板保存一次（旧版白名单漏了 ourcampus）无声造成的。
    # 所以留一条 WARNING：影子名单里的 source 不在 sources 里，一定要说出来。
    _shadow_raw = _parse_sources_raw(os.environ.get("SHADOW_SOURCES", ""))
    shadow_sources = [s for s in _shadow_raw if s in sources]
    _shadow_dangling = [s for s in _shadow_raw if s not in sources]
    if _shadow_dangling:
        logger.warning(
            "SHADOW_SOURCES 里的 %s 不在 SOURCES 中，已忽略——这几个平台"
            "既不会被抓取，也不会出现在数据健康面板上。若想让它们跑起来，"
            "要先加进 SOURCES；若只是残留配置，从 SHADOW_SOURCES 里删掉。",
            ", ".join(sorted(_shadow_dangling)),
        )

    shard_sizes = _parse_shard_sizes(os.environ.get("SHARD_SIZES", ""))
    source_min_intervals = _parse_source_min_intervals(
        os.environ.get("SOURCE_MIN_INTERVALS", ""))

    ourcampus_cities: list[OurCampusCityFilter] = []
    if "ourcampus" in sources:
        raw_oc_cities = os.environ.get(
            "OURCAMPUS_CITIES", "OurCampus Amsterdam Diemen,diemen"
        )
        ourcampus_cities = _parse_ourcampus_cities(raw_oc_cities)

    xior_cities: list[XiorCityFilter] = []
    if "xior" in sources:
        raw_xior_cities = os.environ.get("XIOR_CITIES", "")
        if not raw_xior_cities:
            # 未显式配置时使用荷兰核心楼栋。
            xior_cities = [
                XiorCityFilter(
                    name=c.get("name") or f"{c.get('city', '').strip()} {c.get('bldg', '').strip()}".strip(),
                    key=c["key"],
                )
                for c in KNOWN_XIOR_CITIES
            ]
        else:
            xior_cities = _parse_xior_cities(raw_xior_cities)

    db_path = resolve_project_path(os.environ.get("DB_PATH", "data/listings.db"))
    log_level = (os.environ.get("LOG_LEVEL") or "INFO").upper()

    timezone_str = os.environ.get("TIMEZONE", "Europe/Amsterdam")
    # 启动时校验时区标识符合法性，失败立即报错而非延迟到首次图表查询
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        ZoneInfo(timezone_str)
    except (ZoneInfoNotFoundError, KeyError):
        raise ValueError(f"无效的 IANA 时区标识符: {timezone_str}")

    return Config(
        check_interval=interval,
        cities=cities,
        availability_filters=availability_filters,
        db_path=db_path,
        log_level=log_level,
        peak_interval=int(os.environ.get("PEAK_INTERVAL") or "60"),
        peak_start=os.environ.get("PEAK_START") or "08:30",
        peak_end=os.environ.get("PEAK_END") or "10:00",
        peak_start_2=os.environ.get("PEAK_START_2") or "13:30",
        peak_end_2=os.environ.get("PEAK_END_2") or "15:00",
        peak_weekdays_only=(os.environ.get("PEAK_WEEKDAYS_ONLY") or "true").lower() != "false",
        min_interval=max(5, int(os.environ.get("MIN_INTERVAL") or "15")),
        jitter_ratio=max(0.0, min(0.5, float(os.environ.get("JITTER_RATIO") or "0.20"))),
        timezone=timezone_str,
        heartbeat_interval_minutes=max(0, int(os.environ.get("HEARTBEAT_INTERVAL_MINUTES") or "60")),
        sources=sources,
        shadow_sources=shadow_sources,
        shard_sizes=shard_sizes,
        source_min_intervals=source_min_intervals,
        ourdomain_cities=ourdomain_cities,
        ourcampus_cities=ourcampus_cities,
        xior_cities=xior_cities,
    )
