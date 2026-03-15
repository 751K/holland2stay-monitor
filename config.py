from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from dotenv import load_dotenv

if TYPE_CHECKING:
    from models import Listing

load_dotenv()

BASE_URL = "https://www.holland2stay.com/residences"

# 所有可用城市（从 Holland2Stay GraphQL aggregations 获取，按名称排序）
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
    name: str   # "Eindhoven"
    id: int     # 29


@dataclass
class AvailabilityFilter:
    label: str  # "Available to book"
    id: int     # 179


@dataclass
class ListingFilter:
    """
    可选的通知过滤条件。所有字段留空/None 表示不限制。
    过滤只影响通知，所有房源仍会写入数据库。
    """
    max_rent: Optional[float] = None          # 最高月租，e.g. 1200.0
    min_area: Optional[float] = None          # 最小面积（m²），e.g. 20.0
    max_area: Optional[float] = None          # 最大面积（m²）
    min_floor: Optional[int] = None           # 最低楼层（0=地面层），e.g. 1
    allowed_occupancy: list[str] = field(default_factory=list)   # e.g. ["One", "Two (only couples)"]
    allowed_types: list[str] = field(default_factory=list)       # e.g. ["Studio", "1", "Loft (open bedroom area)"]
    allowed_neighborhoods: list[str] = field(default_factory=list)  # e.g. ["Strijp", "Centrum"]

    def is_empty(self) -> bool:
        """没有设置任何条件，全部放行。"""
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
        """返回 True 表示该房源满足过滤条件，应当发送通知。"""
        fm = listing.feature_map()

        # 租金
        if self.max_rent is not None:
            price = listing.price_value
            if price is not None and price > self.max_rent:
                return False

        # 面积
        area_str = fm.get("area", "")
        area = _parse_float(area_str)
        if area is not None:
            if self.min_area is not None and area < self.min_area:
                return False
            if self.max_area is not None and area > self.max_area:
                return False

        # 楼层
        if self.min_floor is not None:
            floor = _parse_int(fm.get("floor", ""))
            if floor is not None and floor < self.min_floor:
                return False

        # 入住人数类型
        if self.allowed_occupancy:
            occ = fm.get("occupancy", "")
            if not any(a.lower() in occ.lower() for a in self.allowed_occupancy):
                return False

        # 户型
        if self.allowed_types:
            rtype = fm.get("type", "")
            if not any(a.lower() in rtype.lower() for a in self.allowed_types):
                return False

        # 片区
        if self.allowed_neighborhoods:
            nbhd = fm.get("neighborhood", "")
            if not any(a.lower() in nbhd.lower() for a in self.allowed_neighborhoods):
                return False

        return True


def _parse_float(s: str) -> Optional[float]:
    """从 "20 m²" / "29.1 m²" 中解析出数字。"""
    import re
    m = re.search(r"[\d]+\.?\d*", s.replace(",", "."))
    return float(m.group()) if m else None


def _parse_int(s: str) -> Optional[int]:
    import re
    m = re.search(r"\d+", s)
    return int(m.group()) if m else None


@dataclass
class AutoBookConfig:
    """
    自动预订配置。
    enabled=False 时整个自动预订功能关闭。
    dry_run=True 时走完流程但不实际调用 addNewBooking（用于测试）。
    listing_filter 独立于通知过滤，可以设置更严格的条件。
    """
    enabled: bool = False
    dry_run: bool = True          # 默认 dry_run，需显式设置 AUTO_BOOK_DRY_RUN=false 才真正执行
    email: str = ""
    password: str = ""
    listing_filter: ListingFilter = field(default_factory=ListingFilter)


@dataclass
class Config:
    """全局配置：轮询、城市、数据库。通知/过滤/预订移至 users.py / data/users.json。"""
    check_interval: int
    cities: list[CityFilter]
    availability_filters: list[AvailabilityFilter]
    db_path: Path
    log_level: str
    # 智能轮询（荷兰时区高峰期加速）
    peak_interval: int = 60           # 高峰期轮询间隔（秒）
    peak_start: str = "08:30"         # 高峰期开始时间（荷兰本地时间，HH:MM）
    peak_end: str = "10:00"           # 高峰期结束时间（荷兰本地时间，HH:MM）
    peak_weekdays_only: bool = True   # 仅工作日启用高峰轮询

    # ------------------------------------------------------------------ #

    def scrape_tasks(self) -> tuple[list[tuple[str, str]], list[str]]:
        city_tasks = [(c.name, str(c.id)) for c in self.cities]
        availability_ids = [str(af.id) for af in self.availability_filters]
        return city_tasks, availability_ids


def load_config() -> Config:
    interval = int(os.environ.get("CHECK_INTERVAL", "300"))

    # 城市列表，格式: "Eindhoven,29|Amsterdam,1"
    cities: list[CityFilter] = []
    raw_cities = os.environ.get("CITIES", "Eindhoven,29")
    for entry in raw_cities.split("|"):
        parts = entry.strip().split(",")
        if len(parts) == 2:
            cities.append(CityFilter(name=parts[0].strip(), id=int(parts[1].strip())))

    # 可用性过滤，格式: "Available to book,179|Available in lottery,336"
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

    db_path = Path(os.environ.get("DB_PATH", "data/listings.db"))
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()

    return Config(
        check_interval=interval,
        cities=cities,
        availability_filters=availability_filters,
        db_path=db_path,
        log_level=log_level,
        # 智能轮询
        peak_interval=int(os.environ.get("PEAK_INTERVAL", "60")),
        peak_start=os.environ.get("PEAK_START", "08:30"),
        peak_end=os.environ.get("PEAK_END", "10:00"),
        peak_weekdays_only=os.environ.get("PEAK_WEEKDAYS_ONLY", "true").lower() != "false",
    )
