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
- **用户级配置**（ListingFilter / AutoBookConfig）：每用户独立，存于 data/users.json，
  在 Web 面板「用户管理」页修改

依赖关系
--------
仅依赖标准库和 python-dotenv，无内部模块依赖。
users.py 和 web.py 都会 import 本模块中的 dataclass。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from dotenv import load_dotenv

if TYPE_CHECKING:
    from models import Listing

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
ENV_PATH = BASE_DIR / ".env"


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

BASE_URL = "https://www.holland2stay.com/residences"

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
    max_rent / min_area / max_area / min_floor 均采用 fail-closed：
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

    max_area: Optional[float] = None
    """最大面积（m²）。高于此值的房源不通知。"""

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

    def is_empty(self) -> bool:
        """所有条件均未设置时返回 True，表示全部放行。"""
        return (
            self.max_rent is None
            and self.min_area is None
            and self.max_area is None
            and self.min_floor is None
            and not self.allowed_occupancy
            and not self.allowed_types
            and not self.allowed_neighborhoods
        )

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
        area = _parse_float(area_str)
        if self.min_area is not None:
            if area is None:
                logger.warning(
                    "过滤拒绝 [%s]: 已设 min_area=%.0f 但面积字段缺失（API 未返回）",
                    listing.name, self.min_area,
                )
                return False
            if area < self.min_area:
                return False
        if self.max_area is not None:
            if area is None:
                logger.warning(
                    "过滤拒绝 [%s]: 已设 max_area=%.0f 但面积字段缺失（API 未返回）",
                    listing.name, self.max_area,
                )
                return False
            if area > self.max_area:
                return False

        if self.min_floor is not None:
            floor_str = fm.get("floor", "")
            floor = _parse_int(floor_str)
            if floor is None:
                logger.warning(
                    "过滤拒绝 [%s]: 已设 min_floor=%d 但楼层字段缺失（API 返回: %r）",
                    listing.name, self.min_floor, floor_str,
                )
                return False
            if floor < self.min_floor:
                return False

        if self.allowed_occupancy:
            occ = fm.get("occupancy", "")
            if not any(a.lower() in occ.lower() for a in self.allowed_occupancy):
                return False

        if self.allowed_types:
            rtype = fm.get("type", "")
            if not any(a.lower() in rtype.lower() for a in self.allowed_types):
                return False

        if self.allowed_neighborhoods:
            nbhd = fm.get("neighborhood", "")
            if not any(a.lower() in nbhd.lower() for a in self.allowed_neighborhoods):
                return False

        return True


def _parse_float(s: str) -> Optional[float]:
    """
    从含单位的字符串中提取浮点数。

    e.g. "26.0 m²" → 26.0，"" → None
    """
    import re
    m = re.search(r"[\d]+\.?\d*", s.replace(",", "."))
    return float(m.group()) if m else None


def _parse_int(s: str) -> Optional[int]:
    """
    从字符串中提取第一个整数。

    e.g. "3" → 3，"Ground floor" → None
    """
    import re
    m = re.search(r"\d+", s)
    return int(m.group()) if m else None


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
    password        : Holland2Stay 账号密码（明文存储于 data/users.json）
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
    """
    enabled: bool = False
    dry_run: bool = True
    email: str = ""
    password: str = ""
    listing_filter: ListingFilter = field(default_factory=ListingFilter)
    cancel_enabled: bool = False
    payment_method: str = "idealcheckout_ideal"


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
    peak_start          : 高峰开始时间（荷兰本地时间 HH:MM），对应 .env PEAK_START
    peak_end            : 高峰结束时间（荷兰本地时间 HH:MM），对应 .env PEAK_END
    peak_weekdays_only  : True 表示仅工作日启用高峰轮询，对应 .env PEAK_WEEKDAYS_ONLY
    min_interval        : 自适应轮询的下限（秒），对应 .env MIN_INTERVAL；
                          高峰期连续成功时间隔会逐步压低，但不会低于此值；
                          建议 ≥ 15s，过低容易触发 429
    jitter_ratio        : 轮询间隔随机抖动比例（0–0.5），对应 .env JITTER_RATIO；
                          e.g. 0.20 表示实际等待时间在基准值 ±20% 范围内随机浮动，
                          避免多实例在同一时刻集中发起请求
    timezone            : IANA 时区标识符，用于图表日期分组和智能轮询时段判定，
                          对应 .env TIMEZONE；默认 Europe/Amsterdam（荷兰时间 CET/CEST）
    """
    check_interval: int
    cities: list[CityFilter]
    availability_filters: list[AvailabilityFilter]
    db_path: Path
    log_level: str
    peak_interval: int = 60
    peak_start: str = "08:30"
    peak_end: str = "10:00"
    peak_weekdays_only: bool = True
    min_interval: int = 15
    jitter_ratio: float = 0.20
    timezone: str = "Europe/Amsterdam"

    def scrape_tasks(self) -> tuple[list[tuple[str, str]], list[str]]:
        """
        将配置展开为 scraper.scrape_all() 所需的参数格式。

        Returns
        -------
        city_tasks       : [(city_name, city_id_str), ...]
                           e.g. [("Eindhoven", "29"), ("Amsterdam", "24")]
        availability_ids : [id_str, ...]
                           e.g. ["179", "336"]
        """
        city_tasks = [(c.name, str(c.id)) for c in self.cities]
        availability_ids = [str(af.id) for af in self.availability_filters]
        return city_tasks, availability_ids


def load_config() -> Config:
    """
    从环境变量（已由 dotenv 加载）构造并返回 Config 实例。

    读取的 .env 键
    --------------
    CHECK_INTERVAL          int，默认 300
    CITIES                  格式 "城市名,ID|城市名,ID"，默认 "Eindhoven,29"
    AVAILABILITY_FILTERS    格式 "标签,ID|标签,ID"，默认包含 179 和 336
    DB_PATH                 str，默认 "data/listings.db"
    LOG_LEVEL               str，默认 "INFO"
    PEAK_INTERVAL           int，默认 60
    PEAK_START              str HH:MM，默认 "08:30"
    PEAK_END                str HH:MM，默认 "10:00"
    PEAK_WEEKDAYS_ONLY      "true"/"false"，默认 "true"
    MIN_INTERVAL            int ≥ 5，默认 "15"（自适应下限，不低于此值）
    JITTER_RATIO            float 0–0.5，默认 "0.20"
    TIMEZONE                IANA 时区，默认 "Europe/Amsterdam"（荷兰 CET/CEST）

    Raises
    ------
    ValueError  若 CITIES 或 AVAILABILITY_FILTERS 中的 ID 不是合法整数
    ValueError  若 TIMEZONE 不是合法的 IANA 时区标识符
    """
    interval = int(os.environ.get("CHECK_INTERVAL", "300"))

    cities: list[CityFilter] = []
    raw_cities = os.environ.get("CITIES", "Eindhoven,29")
    for entry in raw_cities.split("|"):
        parts = entry.strip().split(",")
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

    db_path = resolve_project_path(os.environ.get("DB_PATH", "data/listings.db"))
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()

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
        peak_interval=int(os.environ.get("PEAK_INTERVAL", "60")),
        peak_start=os.environ.get("PEAK_START", "08:30"),
        peak_end=os.environ.get("PEAK_END", "10:00"),
        peak_weekdays_only=os.environ.get("PEAK_WEEKDAYS_ONLY", "true").lower() != "false",
        min_interval=max(5, int(os.environ.get("MIN_INTERVAL", "15"))),
        jitter_ratio=max(0.0, min(0.5, float(os.environ.get("JITTER_RATIO", "0.20")))),
        timezone=timezone_str,
    )
