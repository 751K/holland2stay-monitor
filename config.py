"""
config.py — 进程级配置
======================
职责
----
1. 项目路径与 .env 的读写（BASE_DIR / ENV_PATH / write_env_key）
2. **import 时加载 .env**，并记录启动时的部署配置
3. 定义全局运行参数 ``Config``，``load_config()`` 从环境变量构造它

不在这里的
----------
- 平台的城市 / 楼栋注册表、楼盘归一：``mcore/registry.py``
- 平台能力与过滤维度表：``mcore/sources.py``
- 用户级配置 ``ListingFilter`` / ``AutoBookConfig``：``filters.py``
  （每用户独立，存于 SQLite user_configs，在「用户管理」页修改）
- 过滤维度适用范围的界面文案：``app/filter_scope.py``

以上名字大多仍从本模块 re-export，旧调用点不用改；新代码直接从各自的模块导入，
只想用 ``ListingFilter`` 的模块这样就不会顺带触发 .env 加载。
"""
from __future__ import annotations

from env_utils import DEFAULT_TIMEZONE, env_bool, env_int


import logging
import os
import sys

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from dotenv import load_dotenv

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
from target_config import (
    parse_availability, parse_cities, parse_source_map, parse_sources, parse_targets,
)
from filters import (  # noqa: F401  （从 config 拆出，保留 re-export）
    ENERGY_LABELS,
    AutoBookConfig,
    ListingFilter,
    _EXACT_MATCH_DIMS,
    energy_rank,
    whitelist_matches,
)
from mcore.registry import (  # noqa: F401  （从 config 拆出，保留 re-export）
    FIN_FULLY as _FIN_FULLY,
    FIN_FURNISHED as _FIN_FURNISHED,
    FIN_SEMI as _FIN_SEMI,
    FIN_UNFURNISHED as _FIN_UNFURNISHED,
    KNOWN_CITIES,
    KNOWN_MAGIS_CITIES,
    KNOWN_OURCAMPUS_CITIES,
    KNOWN_OURDOMAIN_CITIES,
    KNOWN_PLAZA_CITIES,
    KNOWN_STUDENTEXPERIENCE_CITIES,
    KNOWN_XIOR_CITIES,
    TARGET_SPECS,
    XIOR_BUILDING_FURNISHING,
    _CITY_ALIAS_MAP,
    canonical_city,
    known_city_names,
    target_name,
    xior_furnishing_for,
)


if TYPE_CHECKING:
    from scrapers.base import ScrapeTask

logger = logging.getLogger(__name__)

if getattr(sys, "frozen", False):
    # 持久化数据存放到用户目录，保证 web 和 monitor 进程共享同一份数据
    BASE_DIR = Path.home() / ".h2s-monitor"
    ASSETS_DIR = Path(sys._MEIPASS).resolve()
else:
    BASE_DIR = Path(__file__).resolve().parent
    ASSETS_DIR = BASE_DIR

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


# Preserve genuine process overrides before .env adds its own values.
from settings_store import capture_external_runtime
from mcore.config_status import capture_startup as _capture_startup_fixed, file_stamp as _config_file_stamp
capture_external_runtime()
try:
    _startup_dotenv_stamp = _config_file_stamp(ENV_PATH)
except OSError:
    _startup_dotenv_stamp = None
load_dotenv(dotenv_path=ENV_PATH)
try:
    _capture_startup_fixed(ENV_PATH, expected_stamp=_startup_dotenv_stamp)
except (OSError, UnicodeError):
    logging.getLogger(__name__).warning("无法记录启动时部署配置", exc_info=True)

# DB_PATH / TIMEZONE 在模块级定义，作为唯一来源。
# load_config() 和 web.py 均从此处引用，不再各自读 os.environ。
# 注意：必须在 load_dotenv() 和 resolve_project_path() 之后定义，
# 确保 .env 已加载、函数已可用。
DB_PATH  = resolve_project_path(os.environ.get("DB_PATH", "data/listings.db"))
TIMEZONE = os.environ.get("TIMEZONE", DEFAULT_TIMEZONE)
# 数据目录：PID / 重载请求文件、web.log、浏览器 profile、证件暂存等。web 与
# monitor 进程都从这里取，所以只能在这一处读环境变量（web.py 曾经单独读过一次，
# 结果设了 DATA_DIR 只有 web.log 跟着走）。相对路径同样按项目根解释。
DATA_DIR = resolve_project_path(os.environ.get("DATA_DIR") or (BASE_DIR / "data"))

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
class TargetFilter:
    """H2S 之外各平台的单个订阅目标（楼盘或城市）。

    ``key`` 是平台自己的标识，含义见 ``mcore.registry`` 里对应的注册表。
    """
    name: str
    key: str


# 各平台曾各有一个字段完全相同的 dataclass，保留旧名作别名。
OurDomainCityFilter = TargetFilter
OurCampusCityFilter = TargetFilter
MagisCityFilter = TargetFilter
PlazaCityFilter = TargetFilter
StudentExperienceCityFilter = TargetFilter
XiorCityFilter = TargetFilter


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
    timezone: str = DEFAULT_TIMEZONE
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
        因此不在其中（见 ARCHITECTURE 的房源状态与完整性）。
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
        for source, spec in TARGET_SPECS.items():
            if source not in self.sources:
                continue
            by_key = {r["key"]: r.get("city", "") for r in spec.registry}
            names = {by_key.get(t.key) or canonical_city(t.name)
                     for t in self.targets_for(source)}
            names = {n for n in names if n}
            if names:
                out[source] = sorted(names)
        return out

    def targets_for(self, source: str) -> list[TargetFilter]:
        """H2S 之外某个平台的订阅目标；未登记的平台返回空表。"""
        spec = TARGET_SPECS.get(source)
        return getattr(self, spec.config_field) if spec else []

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

        # Magis / Student Experience / Plaza 的每个 task 只是「从同一份结果里取这个
        # 城市」，不各自发请求；整批共用一次抓取，见各自 scraper 的 batch_session。
        for source in TARGET_SPECS:
            if source not in self.sources:
                continue
            tasks.extend(
                ScrapeTask(source=source, city_key=c.key, city_display=c.name)
                for c in self.targets_for(source)
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
    return [
        cls(name=name, key=key)
        for name, key in _target_parse(
            env_key, lambda v: parse_targets(env_key, v), raw)
    ]


def _load_targets(spec, environ) -> list[TargetFilter]:
    """按 ``TargetSpec`` 的约定解析一个平台的订阅串。

    留空且 ``all_when_empty`` 时取注册表全部；键不存在时用 ``default_raw``。
    """
    raw = environ.get(spec.env_key, spec.default_raw)
    if not raw and spec.all_when_empty:
        return [TargetFilter(name=target_name(c), key=c["key"])
                for c in spec.registry]
    return _target_city_filters(TargetFilter, spec.env_key, raw)


def load_config(*, environ=None) -> Config:
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
    if environ is None:
        from settings_store import runtime_environment
        environ = runtime_environment()

    interval = env_int("CHECK_INTERVAL", 300, strict=True, environ=environ)
    _raw_sources = environ.get("SOURCES", "holland2stay")
    sources = _target_parse("SOURCES", parse_sources, _raw_sources)
    if not sources and not _raw_sources.strip():
        sources = ["holland2stay"]

    cities: list[CityFilter] = [
        CityFilter(name=name, id=city_id)
        for name, city_id in _target_parse(
            "CITIES", parse_cities, environ.get("CITIES", "Eindhoven,29"))
    ]

    availability_filters: list[AvailabilityFilter] = [
        AvailabilityFilter(label=label, id=fid)
        for label, fid in _target_parse(
            "AVAILABILITY_FILTERS", parse_availability,
            environ.get(
                "AVAILABILITY_FILTERS",
                "Available to book,179|Available in lottery,336"))
    ]

    targets = {
        source: _load_targets(spec, environ) if source in sources else []
        for source, spec in TARGET_SPECS.items()
    }

    # 影子 source：抓但不通知。只保留同时也在 sources 里的，否则是配置笔误。
    #
    # 但"静默丢弃"会造出一个很难看穿的假象：SHADOW_SOURCES 里列着 ourcampus，
    # 而 SOURCES 里没有它——看配置像是"影子模式跑着"，实际上它压根没被抓，
    # 数据健康面板上自然也没有它的卡片。2026-08-04 线上就是这个状态，
    # 而且是从设置面板保存一次（旧版白名单漏了 ourcampus）无声造成的。
    # 所以留一条 WARNING：影子名单里的 source 不在 sources 里，一定要说出来。
    _shadow_raw = _target_parse(
        "SHADOW_SOURCES", parse_sources, environ.get("SHADOW_SOURCES", ""))
    shadow_sources = [s for s in _shadow_raw if s in sources]
    _shadow_dangling = [s for s in _shadow_raw if s not in sources]
    if _shadow_dangling:
        logger.warning(
            "SHADOW_SOURCES 里的 %s 不在 SOURCES 中，已忽略——这几个平台"
            "既不会被抓取，也不会出现在数据健康面板上。若想让它们跑起来，"
            "要先加进 SOURCES；若只是残留配置，从 SHADOW_SOURCES 里删掉。",
            ", ".join(sorted(_shadow_dangling)),
        )

    shard_sizes = _parse_shard_sizes(environ.get("SHARD_SIZES", ""))
    source_min_intervals = _parse_source_min_intervals(
        environ.get("SOURCE_MIN_INTERVALS", ""))
    source_peak_min_intervals = _parse_source_min_intervals(
        environ.get("SOURCE_PEAK_MIN_INTERVALS", ""),
        _DEFAULT_SOURCE_PEAK_MIN_INTERVALS)

    db_path = resolve_project_path(environ.get("DB_PATH", "data/listings.db"))
    log_level = (environ.get("LOG_LEVEL") or "INFO").upper()

    timezone_str = environ.get("TIMEZONE", DEFAULT_TIMEZONE)
    # 启动时校验时区标识符合法性，失败立即报错而非延迟到首次图表查询
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        ZoneInfo(timezone_str)
    except (ZoneInfoNotFoundError, KeyError):
        raise ValueError(f"无效的 IANA 时区标识符: {timezone_str}") from None

    return Config(
        check_interval=interval,
        cities=cities,
        availability_filters=availability_filters,
        db_path=db_path,
        log_level=log_level,
        peak_interval=env_int("PEAK_INTERVAL", 60, strict=True, environ=environ),
        peak_start=environ.get("PEAK_START") or "08:30",
        peak_end=environ.get("PEAK_END") or "10:00",
        peak_start_2=environ.get("PEAK_START_2") or "13:30",
        peak_end_2=environ.get("PEAK_END_2") or "15:00",
        peak_weekdays_only=env_bool("PEAK_WEEKDAYS_ONLY", True, environ=environ),
        min_interval=env_int("MIN_INTERVAL", 15, minimum=5, strict=True, environ=environ),
        jitter_ratio=max(0.0, min(0.5, float(environ.get("JITTER_RATIO") or "0.20"))),
        timezone=timezone_str,
        heartbeat_interval_minutes=env_int("HEARTBEAT_INTERVAL_MINUTES", 60, minimum=0, strict=True, environ=environ),
        sources=sources,
        shadow_sources=shadow_sources,
        shard_sizes=shard_sizes,
        source_min_intervals=source_min_intervals,
        source_peak_min_intervals=source_peak_min_intervals,
        **{spec.config_field: targets[source]
           for source, spec in TARGET_SPECS.items()},
    )
