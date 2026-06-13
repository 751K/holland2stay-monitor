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

from models import parse_float, parse_int


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


def get_proxy_url() -> str:
    """
    统一的代理 URL 读取，**带故障切换**。

    返回当前**未在冷却**的第一个代理（主代理优先；主代理被
    ``report_proxy_failure`` 标记故障后自动落到备用）。全部都在冷却时返回
    空串，表示抓取临时降级为直连服务器原生 IP。无配置也返回空串。

    所有需要代理的模块（scraper、booker、monitor）均通过此函数获取。
    """
    pool = _proxy_pool()
    if not pool:
        return ""
    now = _time.monotonic()
    for p in pool:
        if _proxy_cooldown_until.get(p, 0.0) <= now:
            return p
    # 全在冷却——降级为直连原生 IP；monitor 会降频到最多 10 min 一次。
    return ""


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
            contract = fm.get("contract", "")
            if not any(a.lower() in contract.lower() for a in self.allowed_contract):
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
    - Xior：``xior_email`` / ``xior_password``
    - OurDomain：``ourdomain_email`` / ``ourdomain_password``

    RENTCafe 平台只存账号凭据——用户须自行在浏览器注册账号，个人信息
    （姓名/电话/出生日期）注册时已录入 RENTCafe，booker 登录后无需再填。

    迁移：旧版共用一套 ``email``/``password``，``users._ab_from_dict`` 在加载时
    把旧值回填进各平台 email/password 字段（见该函数）。
    """
    enabled: bool = False
    dry_run: bool = True
    listing_filter: ListingFilter = field(default_factory=ListingFilter)
    cancel_enabled: bool = False

    # ── H2S ──
    email: str = ""
    password: str = ""
    payment_method: str = "idealcheckout_ideal"

    # ── Xior（RENTCafe 租户 xiorstudenthousing）──
    xior_email: str = ""
    xior_password: str = ""

    # ── OurDomain（RENTCafe 租户 thisisourdomain）──
    ourdomain_email: str = ""
    ourdomain_password: str = ""


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
    ourdomain_cities: list[OurDomainCityFilter] = field(default_factory=list)
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


def _parse_sources(raw: str) -> list[str]:
    values = [
        p.strip().lower()
        for p in re.split(r"[,|]", raw or "")
        if p.strip()
    ]
    return list(dict.fromkeys(values)) or ["holland2stay"]


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
        ourdomain_cities=ourdomain_cities,
        xior_cities=xior_cities,
    )
