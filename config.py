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

from env_utils import env_bool, env_int


import logging
import os
import re
import sys

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from dotenv import load_dotenv

from models import canonical_feature, parse_float, parse_int
from mcore.sources import (  # noqa: F401  （从 config 拆出，保留 re-export）
    KNOWN_SOURCES,
    SOURCE_ASSUMED_FEATURES,
    SOURCE_DISPLAY_NAMES,
    _SOURCE_FILTER_DIMS,
    _UNIVERSAL_FILTER_DIMS,
    _source_supports_dim,
    assumed_features,
    filter_dim_sources,
    source_display_name,
    source_supports_dim,
    sources_supporting_dim,
)


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
    from bookers.applicant_profile import ApplicantProfile
    from models import Listing
    from scrapers.base import ScrapeTask

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
CLOAKBROWSER_HEADLESS = env_bool("CLOAKBROWSER_HEADLESS", True)
"""True=无头模式（Docker/生产），False=可视化（本地调试）"""

# curl_cffi TLS 指纹模拟池，绕过 Cloudflare WAF。
# 代理池 / 故障切换 / TLS 指纹池已搬到 mcore.proxy。保留 re-export，
# 旧调用点（config.get_proxy_url 等）与测试不用一次性改完。
from mcore.proxy import (  # noqa: F401  （兼容旧调用点）
    _CURL_IMPERSONATE_POOL,
    _POOL_WEIGHTS,
    _PROXY_ACCOUNT_COOLDOWN_SEC,
    _PROXY_COOLDOWN_SEC,
    _PROXY_FAILURE_CONFIRM_THRESHOLD,
    _PROXY_FAILURE_CONFIRM_WINDOW_SEC,
    _PROXY_REJECT_HINTS,
    _STICKY_SESSION_RE,
    _derive_session_id,
    _env_int_positive,
    _last_impersonate,
    _personal_proxies,
    _proxy_cooldown_until,
    _proxy_failure_marks,
    _proxy_pool,
    _record_proxy_failure_mark,
    _rotating_counter,
    _same_proxy_endpoint,
    _time,
    _with_source_session,
    get_impersonate,
    get_proxy_url,
    is_personal_proxy_active,
    is_proxy_in_cooldown,
    is_proxy_native_fallback_active,
    probe_proxy,
    proxy_failure_mark_count,
    proxy_pool_size,
    report_proxy_failure,
)

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

# ``city`` 是该地点所属的城市，与 ``name``（平台自己的楼盘叫法）分开。
# 两者混用过：`city` 列里 H2S 存的是真城市，Xior / OurDomain / OurCampus 存的
# 是楼盘名，而筛选是精确匹配——勾「Amsterdam」收不到「Amsterdam Diemen」，
# 2026-08-05 查出线上 14 个用户因此长期漏收通知。见 canonical_city()。
#
# Diemen 行政上是独立市镇，这里仍归 Amsterdam：平台按 Amsterdam 卖，用户也
# 是按 Amsterdam 找房。改归属只需改这一处。
KNOWN_OURCAMPUS_CITIES: list[dict] = [
    {"name": "OurCampus Amsterdam Diemen", "key": "diemen", "city": "Amsterdam"},
]


#: Magis 覆盖的城市。
#:
#: Magis 与其余三个平台不同：一次 ``/for-rent`` 请求返回**全部城市**的全部单元，
#: 不按城市分请求。这里登记城市只是为了让用户能按城市订阅、并让抓下来的整页在
#: 各 ScrapeTask 之间按城市切分——真正的 HTTP 每轮只有一次（见 scrapers/magis.py
#: 的 batch_session）。
#:
#: 五城 17 栋（2026-09-01 侦察）：Eindhoven 9、Tilburg 5，其余三城各 1。
KNOWN_MAGIS_CITIES: list[dict] = [
    {"name": "Eindhoven",         "key": "eindhoven",        "city": "Eindhoven"},
    {"name": "Tilburg",           "key": "tilburg",          "city": "Tilburg"},
    {"name": "'s-Hertogenbosch",  "key": "s-hertogenbosch",  "city": "'s-Hertogenbosch"},
    {"name": "Amersfoort",        "key": "amersfoort",       "city": "Amersfoort"},
    {"name": "Rijswijk",          "key": "rijswijk",         "city": "Rijswijk"},
]


#: Student Experience 覆盖的城市。
#:
#: 与 Magis 同一形态：一轮抓取就拿到全部楼盘，登记城市只为让用户按城市订阅并在
#: 各 ScrapeTask 之间切分（见 scrapers/studentexperience.py 的 batch_session）。
#:
#: 荷兰五处在营，Amsterdam 占四处（Minervahaven / Zuidas / NDSM / Amstel），
#: Leiden 一处。Amstelveen Uilenstede 站点标注 "Under development"，未开放预订，
#: 因此不登记——登记了只会让用户勾一个永远不出房源的城市。
KNOWN_STUDENTEXPERIENCE_CITIES: list[dict] = [
    {"name": "Amsterdam", "key": "amsterdam", "city": "Amsterdam"},
    {"name": "Leiden",    "key": "leiden",    "city": "Leiden"},
]


#: Plaza 覆盖的荷兰城市（2026-09-02 快照）。
#:
#: 与 Magis / Student Experience 同一形态：一个 POST 就返回全站，登记城市只为让
#: 用户按城市订阅并在各 ScrapeTask 之间切分。
#:
#: ⚠️ **这份清单一定会漂。** 它是站点导航自述的八个城市与当时实际在架的十个城市
#: 取并集——两份本来就对不上：在架的 Geldrop（当时 6 条，第二多）、Groot-Ammers、
#: Deventer、Duivendrecht 都不在导航里。站点上架新城市时不会有人来改这里，因此
#: scrapers/plaza.py 在遇到未登记城市时按 WARNING 记下城市名，日志里看得见。
#:
#: 漂移记录（这条机制确实在工作，别删）：
#: 2026-09-08 生产日志报出 Rijswijk（4 条，当时全站第五多），导航里仍然没有它。
#:
#: 这份清单是**唯一**一份 Plaza 城市表——``scrapers/plaza.py`` 的 ``CITIES``
#: 从这里派生，不要在那边另抄一份。抄了就会出现「加进 config 但 WARNING 照报」
#: 或者更糟的「WARNING 不报了但房源仍然没人接」。
KNOWN_PLAZA_CITIES: list[dict] = [
    {"name": "Utrecht",      "key": "utrecht",      "city": "Utrecht"},
    {"name": "Amsterdam",    "key": "amsterdam",    "city": "Amsterdam"},
    {"name": "Eindhoven",    "key": "eindhoven",    "city": "Eindhoven"},
    {"name": "Delft",        "key": "delft",        "city": "Delft"},
    {"name": "Enschede",     "key": "enschede",     "city": "Enschede"},
    {"name": "Maastricht",   "key": "maastricht",   "city": "Maastricht"},
    {"name": "Breda",        "key": "breda",        "city": "Breda"},
    {"name": "Arnhem",       "key": "arnhem",       "city": "Arnhem"},
    {"name": "Geldrop",      "key": "geldrop",      "city": "Geldrop"},
    {"name": "Deventer",     "key": "deventer",     "city": "Deventer"},
    {"name": "Duivendrecht", "key": "duivendrecht", "city": "Duivendrecht"},
    {"name": "Groot-Ammers", "key": "groot-ammers", "city": "Groot-Ammers"},
    {"name": "Rijswijk",     "key": "rijswijk",     "city": "Rijswijk"},
]


KNOWN_OURDOMAIN_CITIES: list[dict] = [
    {"name": "Amsterdam Diemen",    "key": "diemen",     "city": "Amsterdam"},
    {"name": "Amsterdam South-East","key": "south-east", "city": "Amsterdam"},
]


@dataclass
class CityFilter:
    """GraphQL city filter 的单个城市条目。"""
    name: str   # 显示名，e.g. "Eindhoven"
    id: int     # GraphQL filter 数值 ID，e.g. 29


#: H2S ``available_to_book`` 属性的全部取值（2026-08-05 从 aggregations 接口取得，
#: 括号内是当时全荷兰的套数）。标签荷英混用是上游本来的样子。
#:
#:   179   Direct te boeken         直接可订        (34)
#:   6204  To be in lottery         即将进入抽签     (7)
#:   336   Beschikbaar in loterij   抽签中          (46)
#:   6203  Reserved                 已预留，下单未付款 (229)
#:   180   Niet beschikbaar         不可用          (12140)
#:   6253  Coming soon              即将上线        (0)
#:
#: 180 是整个存量池——租出去的、没上架的、下架的全归这一档，它不区分原因。
#: 抓它没有意义：仅监控的两个城市就有 2489 条，是当前抓取量的 80 倍。
AVAILABILITY_RESERVED_ID = 6203


@dataclass
class AvailabilityFilter:
    """
    GraphQL available_to_book filter 的单个可用性条目。

    已知 ID 见上方 ``AVAILABILITY_RESERVED_ID`` 处的完整清单。
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


def _build_city_alias_map() -> dict[str, str]:
    """楼盘名 → 所属城市。key 一律小写。

    只收录**已知**地点，不做任何猜测式解析。用前缀去猜城市看着更省事，但
    「Aachen Vaals Katzensprung」的城市是「Aachen Vaals」而不是「Aachen」，
    猜一次错一次；猜错的后果是把房源归到不存在的城市，比不归一还糟。
    """
    alias: dict[str, str] = {}
    for c in KNOWN_XIOR_CITIES:
        city = (c.get("city") or "").strip()
        bldg = (c.get("bldg") or "").strip()
        if not city:
            continue
        display = c.get("name") or f"{city} {bldg}".strip()
        alias[display.lower()] = city
    for group in (KNOWN_OURDOMAIN_CITIES, KNOWN_OURCAMPUS_CITIES):
        for c in group:
            city = (c.get("city") or "").strip()
            if city and c.get("name"):
                alias[c["name"].strip().lower()] = city
    return alias


_CITY_ALIAS_MAP = _build_city_alias_map()


def canonical_city(value: str) -> str:
    """把 `city` 字段归一到真正的城市名。

    存在的理由：`city` 这一列在四个平台上存的不是同一种东西——H2S 存真城市
    （Eindhoven），Xior / OurDomain / OurCampus 存楼盘名（Utrecht Willem
    Dreeslaan、Amsterdam Diemen）。而 `allowed_cities` 是精确匹配，于是勾了
    「Utrecht」的用户永远收不到 Xior 在 Utrecht 那 25 套房的通知，面板上也
    看不出任何异常。

    未收录的值原样返回：宁可维持现状，也不要猜出一个错误的城市。
    """
    if not value:
        return ""
    return _CITY_ALIAS_MAP.get(value.strip().lower(), value.strip())


def known_city_names(sources=None) -> list[str]:
    """所有平台归一之后的城市全集，供筛选下拉使用。

    传 ``sources`` 时按**实际启用的平台**收敛：没抓的平台城市不该出现在
    用户能勾的清单里——勾了也永远不会有房源。``None`` 保持旧的全集行为。
    """
    if sources is None:
        names = {c["name"] for c in KNOWN_CITIES}
        names.update(_CITY_ALIAS_MAP.values())
        return sorted(names)

    enabled = {str(s).strip().lower() for s in sources}
    names: set[str] = set()
    if "holland2stay" in enabled:
        names.update(c["name"] for c in KNOWN_CITIES)
    for src, registry in (
        ("ourdomain", KNOWN_OURDOMAIN_CITIES),
        ("ourcampus", KNOWN_OURCAMPUS_CITIES),
        ("xior", KNOWN_XIOR_CITIES),
        ("magis", KNOWN_MAGIS_CITIES),
        ("studentexperience", KNOWN_STUDENTEXPERIENCE_CITIES),
        ("plaza", KNOWN_PLAZA_CITIES),
    ):
        if src in enabled:
            names.update(c.get("city") or c["name"] for c in registry)
    names.discard("")
    return sorted(names)


@dataclass
class MagisCityFilter:
    """Magis 的单个城市条目。"""
    name: str
    key: str


@dataclass
class PlazaCityFilter:
    """Plaza 的单个城市条目。"""
    name: str
    key: str


@dataclass
class StudentExperienceCityFilter:
    """Student Experience 的单个城市条目。"""
    name: str
    key: str


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
# building 在通用集里，不是逐平台登记的。2026-09-10 实测生产库 836 条房源，
# 835 条带 Building 特征（七个平台全都写），所以它确实通用。
#
# 更重要的是**方向**：登记在通用里，新平台若不写 Building 就匹配不上（fail-closed，
# 用户少收几条）；逐平台登记则是新平台整条跳过（fail-open）——而
# allowed_buildings 的语义是「只要这栋楼」，fail-open 会把用户明确排除掉的楼放行。
# 如果这个过滤器挂在 auto_book 上，那就是替他自动申请一栋他没要的楼。
_EXACT_MATCH_DIMS = frozenset({"finishing"})


def whitelist_matches(pattern: str, value: str, dim: str = "") -> bool:
    """白名单条目 ``pattern`` 是否命中房源取值 ``value``。

    ``dim`` 是维度名，用来查 ``_EXACT_MATCH_DIMS`` 决定匹配方式。传维度名而不是
    传布尔量，是为了让那张表成为唯一真相源——写成布尔量时表可以被清空而行为
    不变，表就退化成了装饰性注释。

    两边都先过 ``canonical_feature``：上游同一属性有荷兰语和英语两版。

    维度不在 ``_EXACT_MATCH_DIMS`` 里时按**词边界**匹配，而不是裸子串。裸子串会把反义词一起
    收走——``"Furnished" in "Unfurnished"`` 为真，勾了「有家具」会收到无家具的
    房源。词边界排掉它（``Unfurnished`` 里 ``furnished`` 前面是字母 n），同时保住
    真正成立的跨平台包含：H2S 的房型写 ``1``，OurDomain 写
    ``1-Bedroom Apartment``，指的是同一种户型。

    在表里的维度要求整体相等：这类维度的每个取值都是一档，档与档之间不该互相
    命中，哪怕字面上一个是另一个的子串。
    """
    p = canonical_feature(pattern).strip()
    v = canonical_feature(value).strip()
    if not p:
        return False
    if dim in _EXACT_MATCH_DIMS:
        return p.casefold() == v.casefold()
    return re.search(rf"(?<!\w){re.escape(p)}(?!\w)", v, re.IGNORECASE) is not None



def dim_scope_note(dim: str, lang: str = "zh") -> str:
    """维度的适用范围提示；全平台通用时返回空串（无需提示）。"""
    supported = sources_supporting_dim(dim)
    if not supported or len(supported) == len(KNOWN_SOURCES):
        return ""
    names = "、" if lang == "zh" else ", "
    listed = names.join(source_display_name(s) for s in supported)
    if lang == "zh":
        return f"仅对 {listed} 生效；其余平台不提供该属性，其房源不受此条件影响"
    return (f"Applies to {listed} only — other platforms do not report this "
            f"attribute, so their listings are unaffected by this filter")


#: 徽标里最多点名几个平台，多出来的折成「等 N 家」。见 dim_scope_badge。
_BADGE_MAX_NAMES = 2


def dim_scope_badge(dim: str, lang: str = "zh") -> str:
    """紧凑版的适用范围标记，给列表页那种横排筛选栏用。

    完整的一句话在那里会把整行撑散，但完全不提又回到「用户不知道」的老问题，
    所以留一个短标记，完整说明放 tooltip。

    **一定要点名平台，不要只报个数。** 原先「仅 3 个平台」既不说是哪 3 个，也
    不说缺谁，读的人只能去开 tooltip；2026-08-21 实际被误读成「和 Holland2Stay
    有关」，而那三个维度恰恰是 Holland2Stay 全都支持的。

    规则是**说短的那一边**——哪边字少就报哪边，信息量一样而字数更省：

        缺的少   → 「Xior 除外」
        支持的少 → 「仅 Holland2Stay」

    比的是**渲染后的字数**，不是平台个数。两者不等价：``occupancy`` 是 3 支持 /
    3 不支持，按个数是平手（旧实现平手时报支持方，得到 34 字），按字数则缺失方
    32 字更短。平台名长短差得很远（"Xior" 4 字 vs "Student Experience" 18 字），
    数个数会挑错边。

    名字数封顶
    ----------
    只列前 ``_BADGE_MAX_NAMES`` 个，其余折成「等 N 家」。这条是随平台数增长的
    刹车：六个平台时中间分裂的维度两边各三个名字，不封顶就是三十多字，塞在
    10px 的 label 后面会把整行顶散（templates/listings.html 的筛选栏）。

    折叠**保留点名**，只是名字不全：「Xior、Magis 等 3 家除外」既说了是谁，也说了
    有几个。这与「不要只报个数」的规矩不冲突——当年被误读的是「仅 3 个平台」那种
    一个名字都没有的写法（2026-08-21），完整清单一直在 tooltip 里。
    """
    supported = sources_supporting_dim(dim)
    if not supported or len(supported) == len(KNOWN_SOURCES):
        return ""
    missing = [s for s in KNOWN_SOURCES if s not in supported]

    def _render(sources: list[str], *, positive: bool) -> tuple[bool, str]:
        """→ (是否折叠过, 徽标文本)。"""
        sep = "、" if lang == "zh" else ", "
        names = [source_display_name(s) for s in sources]
        folded = len(names) > _BADGE_MAX_NAMES
        if folded:
            shown = sep.join(names[:_BADGE_MAX_NAMES])
            listed = (f"{shown} 等 {len(names)} 家" if lang == "zh"
                      else f"{shown} +{len(names) - _BADGE_MAX_NAMES} more")
        else:
            listed = sep.join(names)
        if positive:
            return folded, (f"仅 {listed}" if lang == "zh" else f"{listed} only")
        # 中文不在「家」后面再空一格；平台名以拉丁字母收尾时才需要那个空格。
        gap = "" if lang == "zh" and listed[-1:] in ("家", "）") else " "
        return folded, (f"{listed}{gap}除外" if lang == "zh"
                        else f"Except {listed}")

    # 排序键先看折没折叠：折叠是**丢信息**（清单不全），只有在不折叠那边明显更长
    # 时才值得。finishing 就是这个情形——支持方折叠后 30 字、缺失方完整 31 字，
    # 按纯字数会挑走那个信息不全的，差一个字不值当。
    return min(_render(supported, positive=True),
               _render(missing, positive=False),
               key=lambda r: (r[0], len(r[1])))[1]


#: 平台整体成立、但 feed 里不上报的属性。
#:
#: Xior 与 OurDomain 的房源全部带家具（床、桌、椅、衣柜），也就是 H2S 口径下的
#: ``Furnished``；它们的 feed 只是没有这个字段。此前靠 fail-open 兜底——「平台不
#: 提供该维度就整体放行」——那条规则不区分用户勾的是哪一档，于是勾
#: ``Unfurnished`` 时这些房源也会出现，而它们恰恰不是无家具的。
#:
#: 把事实直接写出来，fail-open 对该维度就不再需要，两个方向都对。
#:
#: **这是运营方给的断言，不是抓来的数据。** 平台改了配置这里不会自动跟着变，
#: 因此只登记「整栋楼统一、不随房源变化」的属性；随房源变化的一律要抓。
#:
#: OurCampus 不在其中：它至今没有返回过任何可订单元，装修档位无从核实（见
#: ARCHITECTURE §9）。宁可让它继续走 fail-open，也不要登记一个没验证过的事实。
_FIN_FULLY = "Fully furnished"
_FIN_FURNISHED = "Furnished"
_FIN_SEMI = "Semi furnished"
_FIN_UNFURNISHED = "Unfurnished"

#: ``{building_key: 档位}``。每一条都对着站点房型页核过，注释写明依据。
#: 新增楼栋**不要凭印象填**——核不了就别登记，留空比填错好。
XIOR_BUILDING_FURNISHING: dict[str, str] = {
    # 2026-08-21 逐栋核对（站点房型卡片的 FEATURES 清单）
    "p0196062": _FIN_FULLY,   # Amsterdam Karspeldreef —— 3 个房型全是 Fully furnished
    "p0196467": _FIN_FULLY,   # Eindhoven Kronehoefstraat —— 3 个房型全是 Fully furnished
    "p0195855": _FIN_FULLY,   # Eindhoven Zernikestraat —— Comfy / Deluxe 均 Fully furnished
    "p0196098": _FIN_FULLY,   # Groningen Eendrachtskade —— Comfy / Deluxe 均 Fully furnished
    "p0196503": _FIN_FULLY,   # Utrecht Willem Dreeslaan —— 仅 Essential，Fully furnished
    # Aachen Vaals Katzensprung —— 站点写的是 "Partially furnished"：床架有、
    # **床垫没有**（"Bed frame - no mattress"），无 Wardrobe。按 models.py 的
    # 术语表这是 Semi furnished（荷兰语 gestoffeerd），不是 Furnished。
    "p0196061": _FIN_SEMI,
    # Amsterdam Naritaweg 不登记：它四个房型分 Furnished / Unfurnished 两档，
    # 由下面的房型名规则逐条判定，登记一个整栋值反而会盖掉正确答案。
}

#: floorplan 名里的档位后缀。Xior 只在同一栋楼同时卖两种时才加后缀。
_FIN_FP_UNFURNISHED = re.compile(r"\bunfurnished\b", re.IGNORECASE)
_FIN_FP_FURNISHED = re.compile(r"\bfurnished\b", re.IGNORECASE)


def xior_furnishing_for(building_key: str, floorplan_name: str) -> Optional[str]:
    """该单元的装修档位；判不出返回 None（调用方不写该字段）。

    房型名优先于楼栋登记：后缀是上游针对**这一个房型**的声明，比整栋的概括更准。
    """
    name = floorplan_name or ""
    if _FIN_FP_UNFURNISHED.search(name):
        return _FIN_UNFURNISHED
    if _FIN_FP_FURNISHED.search(name):
        # 站点上带 "/ Furnished" 后缀的房型，FEATURES 里写的都是 Fully furnished
        return _FIN_FULLY
    return XIOR_BUILDING_FURNISHING.get(building_key)


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

    字符串白名单字段（allowed_occupancy / allowed_types / allowed_neighborhoods /
    allowed_buildings）本身已是 fail-closed：字段缺失时为空字符串，白名单匹配必然失败。

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

    allowed_buildings: list[str] = field(default_factory=list)
    """
    楼盘白名单（子串匹配，大小写不敏感）。非空时只通知指定楼盘的房源。
    e.g. ["Teteringsedijk 120"] → 该楼所有单元；["Laagstraat"] → 该楼盘全部。

    这是「只要这栋楼里的任何一间」那个需求。七个平台都写 ``Building``，取值形态
    各不相同：H2S / Magis 是楼盘名（``Laagstraat`` / ``Novum``），Plaza 是从地址
    推出来的街道或街道+门牌（``Teteringsedijk 120`` 恰好覆盖 B16/B22/B26），
    Student Experience 是「城市 + 楼」（``Amsterdam NDSM``）。所以匹配用子串，
    与其余白名单字段一致；候选值由 ``/api/buildings`` 从库里现取，用户从下拉里
    选，不手打。
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
        #
        # 这些维度**两边都要过 canonical_feature**：上游同一个属性有荷兰语和
        # 英语两版（``Two (only couples)`` / ``Twee (alleen koppels)``），返回哪
        # 一版取决于录入语言，与房源无关。下拉里只保留归一后的写法，而老用户
        # 存下来的值可能是荷兰语原文——只归一一侧仍然会漏。
        # 这些维度并非每个平台都有（见 _SOURCE_FILTER_DIMS）。平台**不支持**该
        # 维度 → 跳过该条件（fail-open，避免一套过滤条件误杀整批房源）；平台
        # 支持但本条缺值 → 白名单匹配失败照常拒绝（不削弱该平台过滤严格度）。
        if self.allowed_occupancy and _source_supports_dim(listing.source, "occupancy"):
            occ = fm.get("occupancy", "")
            if not any(whitelist_matches(a, occ) for a in self.allowed_occupancy):
                return False

        if self.allowed_types and _source_supports_dim(listing.source, "type"):
            rtype = fm.get("type", "")
            if not any(whitelist_matches(a, rtype) for a in self.allowed_types):
                return False

        if self.allowed_buildings and _source_supports_dim(listing.source, "building"):
            bld = fm.get("building", "")
            if not any(whitelist_matches(a, bld) for a in self.allowed_buildings):
                return False

        if self.allowed_neighborhoods and _source_supports_dim(listing.source, "neighborhood"):
            nbhd = fm.get("neighborhood", "")
            if not any(whitelist_matches(a, nbhd) for a in self.allowed_neighborhoods):
                return False

        if self.allowed_cities:
            # 两边都先归一。房源侧的 city 可能是楼盘名（Xior / OurDomain /
            # OurCampus），用户侧的存量配置里也可能存着楼盘名——归一之后
            # 「Utrecht」与「Utrecht Willem Dreeslaan」才落在同一个值上。
            # 只归一一侧仍然会漏，这也是本处原先的写法。
            city = canonical_city(listing.city or "").lower()
            if not any(canonical_city(a).lower() == city for a in self.allowed_cities):
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
            if not any(whitelist_matches(a, contract) for a in self.allowed_contract):
                return False

        if self.allowed_tenant and _source_supports_dim(listing.source, "tenant"):
            tenant = fm.get("tenant", "")
            if not any(whitelist_matches(a, tenant) for a in self.allowed_tenant):
                return False

        if self.allowed_offer and _source_supports_dim(listing.source, "offer"):
            offer = fm.get("offer", "")
            if not any(whitelist_matches(a, offer) for a in self.allowed_offer):
                return False

        if self.allowed_finishing and _source_supports_dim(listing.source, "finishing"):
            furnishing = fm.get("furnishing", "")
            # 装修程度是互斥的四档，见 _EXACT_MATCH_DIMS
            if not any(whitelist_matches(a, furnishing, "finishing")
                       for a in self.allowed_finishing):
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


#: 申请人档案里**不**加密的字段。**默认是加密**，这张表是例外清单。
#:
#: 为什么反过来写
#: --------------
#: 原先是正向白名单，只登记了 date_of_birth / address / id_number 三项，理由是
#: 「只加密真正抬高身份盗用风险的」。但这张档案是给 RENTCafe 租房申请表填的，
#: 剩下的字段包括国籍、出生地、性别、电话、学号，以及
#: ``ever_evicted`` / ``ever_convicted`` / ``criminal_charges``
#: ——**犯罪记录在 GDPR 下属第 10 条数据**，比身份证号更该保护，却一直是明文。
#:
def _default_applicant_profile():
    """延迟导入：bookers 包会 import scrapers→config，不能在 config 顶层导入。"""
    from bookers.applicant_profile import ApplicantProfile
    return ApplicantProfile()



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
    - Plaza：``plaza_username`` / ``plaza_password``（用户名，不是邮箱）

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
    applicant_profile: "ApplicantProfile" = field(default_factory=_default_applicant_profile)

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

    # ── OurCampus（RENTCafe，与 OurDomain 同一套流程，另一个租户）──
    #
    # **面板还没有入口**：``OurCampusBooker`` 写好了但未注册，要等「开始申请能
    # 不能占住名额」实测过再开（见 bookers/__init__.py）。字段先加上，是为了让
    # booker 和探针有正经的出处；表单保存时原样保留，不会被清空。
    ourcampus_email: str = ""
    ourcampus_password: str = ""

    # ── Plaza（Zig/Hexia 门户，用户名不是邮箱）──
    #
    # Plaza 登录用的是**用户名**，不是邮箱（登录表单标签 "Jouw gebruikersnaam"，
    # 站点另外提供「忘记用户名」入口）。所以这里叫 username 而不是 email——
    # 与另外三个平台字段名不同是有意的，照着改成 email 会让人以为能填邮箱。
    #
    # 应征本身**不需要任何资料**：提交只有两个 id，没有表单、证件、IBAN 或支付
    # 方式（见 docs/PLAZA.md §1）。资料在**注册**阶段一次性交给站点（€27.50/年），
    # booker 不碰。
    plaza_username: str = ""
    plaza_password: str = ""
    #: Plaza 自动应征的**显式开关**，默认关（fail-closed）。
    #:
    #: 另外三个平台是「有凭据即参与」——凭据本身就是开关。Plaza 多这一道，是因为
    #: 它和那三个不是一类：H2S 下单后还要付款、Xior / OurDomain 停在存草稿，
    #: 都还有一步在用户手里；Plaza 的应征**一次 POST 就落地**，中间没有任何人工
    #: 关卡。让「填了凭据」顺带等于「授权系统替我应征」，这个跨度太大。
    #:
    #: 凭据仍然是第二道闸：两个都满足才产生候选（monitor._can_auto_book）。
    plaza_enabled: bool = False

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
    #: 高峰期专用的最小间隔；缺项的 source 回落到 ``source_min_intervals``。
    #: 见 _DEFAULT_SOURCE_PEAK_MIN_INTERVALS——这道闸门只在高峰期真正起作用。
    source_peak_min_intervals: dict[str, int] = field(default_factory=dict)
    ourdomain_cities: list[OurDomainCityFilter] = field(default_factory=list)
    ourcampus_cities: list[OurCampusCityFilter] = field(default_factory=list)
    xior_cities: list[XiorCityFilter] = field(default_factory=list)
    magis_cities: list[MagisCityFilter] = field(default_factory=list)
    studentexperience_cities: list[StudentExperienceCityFilter] = field(
        default_factory=list)
    plaza_cities: list[PlazaCityFilter] = field(default_factory=list)

    def sources_with_full_lifecycle(self) -> frozenset[str]:
        """feed 覆盖了「已预留」状态的 source。

        为什么这决定了状态收敛的判据：**「从 feed 里消失」的含义取决于 feed 覆盖
        了什么**。

        - feed 只含可订/抽签时，消失是**有歧义**的——可能被人下单了（Reserved），
          也可能彻底没了。所以先推 Reserved、再推 Occupied，留出付款窗口。
        - feed 也含 Reserved 时，消失就**没有歧义**了：它已经掉出我们跟踪的全部
          状态，只能是 H2S 的 ``Niet beschikbaar``。此时再推一次 Reserved 是凭空
          造一个平台从没说过的状态。

        判据从实际配置推出，不写死平台名——``AVAILABILITY_FILTERS`` 是可改的，
        写死会在别人改配置时静默失准。

        其余三个平台的 feed 只列可订单元，没有等价的「已预留」状态可抓，
        因此不在其中（见 ARCHITECTURE §5.13）。
        """
        ids = {af.id for af in self.availability_filters}
        if AVAILABILITY_RESERVED_ID in ids and "holland2stay" in self.sources:
            return frozenset({"holland2stay"})
        return frozenset()

    def monitored_city_names(self) -> list[str]:
        """当前实际在监控的城市名，去重排序，供面板向用户说明覆盖范围。

        跨 source 取并集并归一化：OurDomain 的「Amsterdam Diemen」、Xior 的
        「Eindhoven Kronehoefstraat」都是楼盘名，对用户而言它们就是 Amsterdam 和
        Eindhoven，分开列只会让人以为监控了七八个城市。

        只算 ``sources`` 里真正启用的平台——某个 source 的城市列表填着但平台没开，
        它一条都不会抓，列出来就是虚报。

        楼盘归属**按 key 查官方注册表**，不对显示名做前缀猜测：``canonical_city``
        走的是显式别名表，表里没有的新楼盘会原样漏出去，横幅上就会出现
        「Maastricht Brusselsepoort」这种半截地名。注册表里本来就带 ``city`` 字段，
        用它是精确的。查不到才退回 ``canonical_city``。
        """
        merged: set[str] = set()
        for cities in self.monitored_cities_by_source().values():
            merged.update(cities)
        return sorted(merged)

    def monitored_cities_by_source(self) -> dict[str, list[str]]:
        """按平台分组的监控城市：``{source: [城市名]}``，只含真正启用的平台。

        为什么要按平台分，而不是并集
        ----------------------------
        并集在三四个平台时还读得动，七个平台之后是一行十七个城市名，读的人既不知道
        哪个城市由谁覆盖，也无从判断自己那个城市的房源大概是什么类型——Plaza 的
        Groot-Ammers 和 Holland2Stay 的 Eindhoven 在同一行里长得一模一样，实际
        一个是一条房源、另一个是几十条。

        楼盘归属按 key 查官方注册表，不对显示名做前缀猜测：``canonical_city`` 走的是
        显式别名表，表里没有的新楼盘会原样漏出去，横幅上就会出现「Maastricht
        Brusselsepoort」这种半截地名。注册表里本来就带 ``city`` 字段，用它是精确的。
        查不到才退回 ``canonical_city``。

        同一平台内部仍然去重归一：OurDomain 的「Amsterdam Diemen」、Xior 的
        「Eindhoven Kronehoefstraat」都是楼盘名，对用户而言就是 Amsterdam 和
        Eindhoven，分开列只会让人以为监控了七八个城市。

        **影子 source 照常返回**，由调用方决定怎么呈现——它们确实在抓、在入库、在
        面板上看得见，只是不发通知。是否要在横幅上标注「暂不推送」是展示层的事，
        不该由这里静默过滤掉：过滤掉的话，用户在房源列表里看见 Plaza 的房子却在
        覆盖说明里找不到那个城市，那是另一种自相矛盾。
        """
        out: dict[str, list[str]] = {}
        if "holland2stay" in self.sources:
            names = {canonical_city(c.name) for c in self.cities}
            if names:
                out["holland2stay"] = sorted(n for n in names if n)
        for source, targets, registry in (
            ("ourdomain", self.ourdomain_cities, KNOWN_OURDOMAIN_CITIES),
            ("ourcampus", self.ourcampus_cities, KNOWN_OURCAMPUS_CITIES),
            ("xior", self.xior_cities, KNOWN_XIOR_CITIES),
            ("magis", self.magis_cities, KNOWN_MAGIS_CITIES),
            ("studentexperience", self.studentexperience_cities,
             KNOWN_STUDENTEXPERIENCE_CITIES),
            ("plaza", self.plaza_cities, KNOWN_PLAZA_CITIES),
        ):
            if source not in self.sources:
                continue
            by_key = {r["key"]: r.get("city", "") for r in registry}
            names = {by_key.get(t.key) or canonical_city(t.name) for t in targets}
            names = {n for n in names if n}
            if names:
                out[source] = sorted(names)
        return out

    def scrape_tasks_v2(self) -> list["ScrapeTask"]:
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

        # Magis 的每个 task 只是「从同一份页面里取这个城市」，不各自发请求；
        # 整批共用一次 HTTP，见 scrapers/magis.py 的 batch_session。
        if "magis" in self.sources:
            tasks.extend(
                ScrapeTask(
                    source="magis",
                    city_key=c.key,
                    city_display=c.name,
                )
                for c in self.magis_cities
            )

        # Student Experience 同 Magis：每个 task 只是从同一轮抓取结果里取这个
        # 城市，不各自发请求。见 scrapers/studentexperience.py 的 batch_session。
        if "studentexperience" in self.sources:
            tasks.extend(
                ScrapeTask(
                    source="studentexperience",
                    city_key=c.key,
                    city_display=c.name,
                )
                for c in self.studentexperience_cities
            )

        # Plaza 同上：一个 POST 拿全站，各 task 只是从同一份结果里取这个城市。
        # 见 scrapers/plaza.py 的 batch_session。
        if "plaza" in self.sources:
            tasks.extend(
                ScrapeTask(
                    source="plaza",
                    city_key=c.key,
                    city_display=c.name,
                )
                for c in self.plaza_cities
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
#: 所以真实可持续值在 90–600 秒之间。600 与 300 实测都干净，180 则**不干净**：
#: 按 task 计的 429，08-18 共 72 次、08-19 共 80 次、08-20 共 39 次。
#:
#: 这个值现在只是**基准**。实际遵守的是它乘上 mcore/pacing.py 的自适应倍率
#: （1 / 2 / 4 倍，即 180 / 360 / 720 秒）——360 与 720 恰好夹住上面那两个
#: 实测干净的值，两次碰撞即可收敛到安全区，之后连续干净若干轮再逐档退回。
#:
#: 原先这里写的是「若 180 也干净，下一档再往 120 试」，隐含假设是「不干净会
#: 有人注意到并退回上一档」。没有人退，于是 180 挂了十天。把这一步交给代码
#: 之后就不再依赖那个假设。
#:
#: **别直接设 0**：那就是回到上表第一行的状态，且会连自适应一起关掉
#: （关掉就是关掉，见 AdaptivePacing.gap_for）。
_DEFAULT_SOURCE_MIN_INTERVALS: dict[str, int] = {"xior": 180}

#: 高峰期专用的最小间隔。留空的 source 沿用上面那份。
#:
#: 为什么需要分时段：这道闸门**只在高峰期起作用**。峰外轮次本身就是
#: ``check_interval``（300 秒起，加抖动实测中位 381 秒），比 180 秒的闸门还慢，
#: 闸门形同不存在；峰内轮次是 ``peak_interval`` 60 秒、自适应还会衰减到
#: ``min_interval`` 20 秒，闸门成了唯一给 Xior 减速的东西。
#:
#: 2026-08-14–21 七天实测（真实窗口 07:30–11:00 / 13:30–17:00，仅工作日）：
#:
#:     ===========  ==========  ============  ==============
#:     时段         轮间隔中位  每轮 429 概率  429 次数
#:     ===========  ==========  ============  ==============
#:     峰内 180 闸  202 秒      12.44%         51
#:     峰外 无闸    381 秒       0.09%          1
#:     ===========  ==========  ============  ==============
#:
#: 节奏只差不到 2 倍，429 概率差 **138 倍**——180 秒就压在阈值的悬崖边上，
#: 而 380 秒在安全侧。98% 的 429 出在那 7 小时里。
#:
#: 所以峰内取 360 秒：贴着实测安全的 381 秒，又不必把峰外一起拖慢。注意方向
#: 和 ``peak_interval`` 相反——那个是高峰**加快**轮次，这个是高峰**放慢**单个
#: source，正因为轮次加快了才需要它。
_DEFAULT_SOURCE_PEAK_MIN_INTERVALS: dict[str, int] = {"xior": 360}


def _parse_source_min_intervals(
    raw: str, defaults: dict[str, int] | None = None,
) -> dict[str, int]:
    """解析 ``SOURCE_MIN_INTERVALS``，形如 ``xior:600,ourdomain:120``（秒）。

    留空用 :data:`_DEFAULT_SOURCE_MIN_INTERVALS`；显式写 ``xior:0`` 关掉该
    source 的节流。解析逻辑统一在 ``target_config.parse_source_map``，这里只负责
    合并默认值。

    ``SOURCE_PEAK_MIN_INTERVALS`` 走同一个解析器，只是换一份默认值。
    """
    from target_config import parse_source_map
    out = dict(_DEFAULT_SOURCE_MIN_INTERVALS if defaults is None else defaults)
    out.update(_target_parse(
        "SOURCE_MIN_INTERVALS",
        lambda v: parse_source_map("SOURCE_MIN_INTERVALS", v), raw))
    return out


def _parse_shard_sizes(raw: str) -> dict[str, int]:
    """解析 ``SHARD_SIZES``，形如 ``xior:5,ourdomain:3``。

    留空用 :data:`_DEFAULT_SHARD_SIZES`；显式写 ``xior:0`` 可以关掉某个 source
    的分片（0 表示不限制）。解析逻辑统一在 ``target_config.parse_source_map``。
    """
    from target_config import parse_source_map
    out = dict(_DEFAULT_SHARD_SIZES)
    out.update(_target_parse(
        "SHARD_SIZES", lambda v: parse_source_map("SHARD_SIZES", v), raw))
    return out




def _target_parse(key: str, parser, raw: str):
    """调 target_config 解析器：格式错误拒绝启动，实体未知只 WARNING。"""
    parsed, problems = parser(raw)
    fatal = [p for p in problems if p.fatal]
    for p in problems:
        if p.fatal:
            logger.error("⚙️  配置格式有误：%s", p)
        else:
            logger.warning("⚙️  配置提示：%s", p)
    if fatal:
        raise ValueError("；".join(str(p) for p in fatal))
    return parsed


def _target_city_filters(cls, env_key: str, raw: str):
    from target_config import parse_targets
    return [
        cls(name=name, key=key)
        for name, key in _target_parse(
            env_key, lambda v: parse_targets(env_key, v), raw)
    ]


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
    from target_config import (
        parse_availability, parse_cities, parse_sources,
    )

    interval = env_int("CHECK_INTERVAL", 300, strict=True)
    _raw_sources = os.environ.get("SOURCES", "holland2stay")
    sources = _target_parse("SOURCES", parse_sources, _raw_sources)
    if not sources and not _raw_sources.strip():
        sources = ["holland2stay"]

    cities: list[CityFilter] = [
        CityFilter(name=name, id=city_id)
        for name, city_id in _target_parse(
            "CITIES", parse_cities, os.environ.get("CITIES", "Eindhoven,29"))
    ]

    availability_filters: list[AvailabilityFilter] = [
        AvailabilityFilter(label=label, id=fid)
        for label, fid in _target_parse(
            "AVAILABILITY_FILTERS", parse_availability,
            os.environ.get(
                "AVAILABILITY_FILTERS",
                "Available to book,179|Available in lottery,336"))
    ]

    ourdomain_cities: list[OurDomainCityFilter] = []
    if "ourdomain" in sources:
        ourdomain_cities = _target_city_filters(
            OurDomainCityFilter, "OURDOMAIN_CITIES",
            os.environ.get("OURDOMAIN_CITIES", "Amsterdam Diemen,diemen"))

    # 影子 source：抓但不通知。只保留同时也在 sources 里的，否则是配置笔误。
    #
    # 但"静默丢弃"会造出一个很难看穿的假象：SHADOW_SOURCES 里列着 ourcampus，
    # 而 SOURCES 里没有它——看配置像是"影子模式跑着"，实际上它压根没被抓，
    # 数据健康面板上自然也没有它的卡片。2026-08-04 线上就是这个状态，
    # 而且是从设置面板保存一次（旧版白名单漏了 ourcampus）无声造成的。
    # 所以留一条 WARNING：影子名单里的 source 不在 sources 里，一定要说出来。
    _shadow_raw = _target_parse(
        "SHADOW_SOURCES", parse_sources, os.environ.get("SHADOW_SOURCES", ""))
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
    source_peak_min_intervals = _parse_source_min_intervals(
        os.environ.get("SOURCE_PEAK_MIN_INTERVALS", ""),
        _DEFAULT_SOURCE_PEAK_MIN_INTERVALS)

    ourcampus_cities: list[OurCampusCityFilter] = []
    if "ourcampus" in sources:
        ourcampus_cities = _target_city_filters(
            OurCampusCityFilter, "OURCAMPUS_CITIES",
            os.environ.get("OURCAMPUS_CITIES",
                           "OurCampus Amsterdam Diemen,diemen"))

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
            xior_cities = _target_city_filters(
                XiorCityFilter, "XIOR_CITIES", raw_xior_cities)

    db_path = resolve_project_path(os.environ.get("DB_PATH", "data/listings.db"))
    log_level = (os.environ.get("LOG_LEVEL") or "INFO").upper()

    timezone_str = os.environ.get("TIMEZONE", "Europe/Amsterdam")
    # 启动时校验时区标识符合法性，失败立即报错而非延迟到首次图表查询
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        ZoneInfo(timezone_str)
    except (ZoneInfoNotFoundError, KeyError):
        raise ValueError(f"无效的 IANA 时区标识符: {timezone_str}") from None

    magis_cities: list[MagisCityFilter] = []
    if "magis" in sources:
        raw_magis_cities = os.environ.get("MAGIS_CITIES", "")
        if raw_magis_cities:
            magis_cities = _target_city_filters(
                MagisCityFilter, "MAGIS_CITIES", raw_magis_cities)
        else:
            # 未显式配置时监控全部五城。Magis 一次请求就返回全站，多监控几个城市
            # 不产生额外请求——按城市裁剪的意义只在于用户订阅的粒度。
            magis_cities = [
                MagisCityFilter(name=c["name"], key=c["key"])
                for c in KNOWN_MAGIS_CITIES
            ]

    studentexperience_cities: list[StudentExperienceCityFilter] = []
    if "studentexperience" in sources:
        raw_se_cities = os.environ.get("STUDENTEXPERIENCE_CITIES", "")
        if raw_se_cities:
            studentexperience_cities = _target_city_filters(
                StudentExperienceCityFilter, "STUDENTEXPERIENCE_CITIES",
                raw_se_cities)
        else:
            # 与 Magis 同理：一轮抓取就拿到全部楼盘，少勾城市不减少请求，
            # 只是不再收这些城市的房源。留空即两城全收。
            studentexperience_cities = [
                StudentExperienceCityFilter(name=c["name"], key=c["key"])
                for c in KNOWN_STUDENTEXPERIENCE_CITIES
            ]

    plaza_cities: list[PlazaCityFilter] = []
    if "plaza" in sources:
        raw_plaza_cities = os.environ.get("PLAZA_CITIES", "")
        if raw_plaza_cities:
            plaza_cities = _target_city_filters(
                PlazaCityFilter, "PLAZA_CITIES", raw_plaza_cities)
        else:
            # 与 Magis / Student Experience 同理：一个 POST 拿全站，少勾城市不减少
            # 请求，只是不再收这些城市的房源。留空即全收。
            plaza_cities = [
                PlazaCityFilter(name=c["name"], key=c["key"])
                for c in KNOWN_PLAZA_CITIES
            ]

    return Config(
        check_interval=interval,
        cities=cities,
        availability_filters=availability_filters,
        db_path=db_path,
        log_level=log_level,
        peak_interval=env_int("PEAK_INTERVAL", 60, strict=True),
        peak_start=os.environ.get("PEAK_START") or "08:30",
        peak_end=os.environ.get("PEAK_END") or "10:00",
        peak_start_2=os.environ.get("PEAK_START_2") or "13:30",
        peak_end_2=os.environ.get("PEAK_END_2") or "15:00",
        peak_weekdays_only=env_bool("PEAK_WEEKDAYS_ONLY", True),
        min_interval=env_int("MIN_INTERVAL", 15, minimum=5, strict=True),
        jitter_ratio=max(0.0, min(0.5, float(os.environ.get("JITTER_RATIO") or "0.20"))),
        timezone=timezone_str,
        heartbeat_interval_minutes=env_int("HEARTBEAT_INTERVAL_MINUTES", 60, minimum=0, strict=True),
        sources=sources,
        shadow_sources=shadow_sources,
        shard_sizes=shard_sizes,
        source_min_intervals=source_min_intervals,
        source_peak_min_intervals=source_peak_min_intervals,
        ourdomain_cities=ourdomain_cities,
        ourcampus_cities=ourcampus_cities,
        xior_cities=xior_cities,
        magis_cities=magis_cities,
        studentexperience_cities=studentexperience_cities,
        plaza_cities=plaza_cities,
    )
