"""
models.py — 核心数据模型
========================
定义 `Listing` dataclass，是整个系统唯一的房源数据载体。
Scraper 生成、Storage 存储、Notifier 格式化、Booker 预订都以此为输入。

不依赖任何其他项目模块（零内部依赖），可单独 import。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

STATUS_AVAILABLE = "available to book"
STATUS_LOTTERY   = "available in lottery"


# LISTING_KEY_MAP：将 GraphQL 属性名映射为 feature_map() 返回的标准 key。
# scraper.py 在构建 features 列表时用 "Type: Studio" 这样的格式，
# feature_map() 再把 "Type" 还原成 "type" 等内部 key。
# 公开导出，供 web.py 的 parse_features 过滤器复用，
# 避免维护多份副本。修改此处即同步所有依赖方。
LISTING_KEY_MAP: dict[str, str] = {
    "Type":         "type",         # 房型，e.g. "Studio" / "1" / "Loft (open bedroom area)"
    "Area":         "area",         # 面积，e.g. "26.0 m²"
    "Occupancy":    "occupancy",    # 入住人数，e.g. "Single" / "Two (only couples)"
    "Floor":        "floor",        # 楼层数字字符串，e.g. "3"
    "Finishing":    "furnishing",   # 装修类型，e.g. "Upholstered" / "Shell"
    "Energy":       "energy_label", # 能耗标签，e.g. "A" / "B"
    "Neighborhood": "neighborhood", # 片区，e.g. "Strijp-S"
    "Building":     "building",     # 楼盘名，e.g. "The Docks"
}


# ------------------------------------------------------------------ #
# 数字解析（模块级，供 config / monitor 复用，避免多处维护正则）
# ------------------------------------------------------------------ #


def parse_float(text: Optional[str]) -> Optional[float]:
    """
    从含单位的字符串中提取浮点数，容忍千分位逗号。

    e.g. "€707" → 707.0, "1,200.50" → 1200.5, "26.0 m²" → 26.0,
         "" → None, None → None
    """
    if not text:
        return None
    m = re.search(r"\d+(?:\.\d+)?", text.replace(",", ""))
    return float(m.group()) if m else None


def parse_int(text: Optional[str]) -> Optional[int]:
    """
    从字符串中提取第一个整数。

    e.g. "3" → 3, "Ground floor" → None
    """
    if not text:
        return None
    m = re.search(r"\d+", text)
    return int(m.group()) if m else None


def parse_features_list(features: list[str]) -> dict[str, str]:
    """将 ["Type: Studio", "Area: 26.0 m²", ...] 解析为 {"type": "Studio", "area": "26.0 m²", ...}。"""
    result: dict[str, str] = {}
    for feat in features:
        if ": " in feat:
            raw_key, value = feat.split(": ", 1)
            result[LISTING_KEY_MAP.get(raw_key, raw_key.lower())] = value
    return result


@dataclass
class Listing:
    """
    单个房源的完整快照。

    字段说明
    --------
    id              URL slug，全局唯一，同时用作数据库主键和 GraphQL url_key。
                    e.g. "kastanjelaan-1-108"
    name            展示名，e.g. "Kastanjelaan 1-108, Eindhoven"
    status          可用性状态，直接来自 GraphQL `available_to_book` 属性的 label。
                    常见值："Available to book" | "Available in lottery" | "Not available"
    price_raw       原始价格字符串，e.g. "€707"（由 scraper 从 basic_rent 属性格式化）
    available_from  入住日期，ISO 格式 "YYYY-MM-DD"，来自 available_startdate 属性
    features        特征列表，格式 ["Type: Studio", "Area: 26.0 m²", "Floor: 3", ...]
                    由 scraper 从多个 custom_attributesV2 属性拼装而来
    url             房源详情页完整 URL
    city            来源城市名，用于多城市监控时区分，e.g. "Eindhoven"
    sku             Magento 内部 SKU，预订时用于 addNewBooking mutation；
                    由 scraper 从 GraphQL 响应直接提取，省去 try_book 中的独立查询
    contract_id     合同类型 ID（来自 type_of_contract 属性）；
                    预订时必须传入，否则 addNewBooking 可能 Internal server error
    contract_start_date  预订用的合同开始日期（来自 next_contract_startdate 属性）；
                    与 available_from 不同：available_from 用于展示/日历，
                    contract_start_date 用于预订 API 调用；可能为 None
    """

    id: str
    name: str
    status: str
    price_raw: Optional[str]
    available_from: Optional[str]
    features: list[str]
    url: str
    city: str = ""
    sku: str = ""
    contract_id: Optional[int] = None
    contract_start_date: Optional[str] = None

    # feature_map() 解析结果缓存，排除在 __repr__ / __eq__ / __init__ 之外
    _feature_map_cache: Optional[dict[str, str]] = field(
        default=None, init=False, repr=False, compare=False
    )

    # ------------------------------------------------------------------ #
    # 计算属性
    # ------------------------------------------------------------------ #

    @property
    def price_value(self) -> Optional[float]:
        """
        从 price_raw 中解析出数字，用于过滤条件比较和排序。

        Returns
        -------
        float 或 None（price_raw 为 None 或无法解析时）
        例：price_raw="€707" → 707.0
        """
        return parse_float(self.price_raw)

    @property
    def price_display(self) -> str:
        """
        提取 price_raw 中的 "€xxx" 部分，供通知消息和 UI 显示使用。

        Returns
        -------
        如 "€707"；无法解析时返回原始字符串或 "价格未知"
        """
        if not self.price_raw:
            return "价格未知"
        m = re.search(r"€[\d,\.]+", self.price_raw)
        return m.group() if m else self.price_raw

    @property
    def is_available(self) -> bool:
        """
        True 表示该房源处于可报名状态（可直接预订或抽签）。

        对应 GraphQL available_to_book 属性的两个合法 label：
          - "Available to book"    → 可直接预订（id=179）
          - "Available in lottery" → 进入抽签池（id=336）
        """
        return self.status.lower() in (STATUS_AVAILABLE, STATUS_LOTTERY)

    def feature_map(self) -> dict[str, str]:
        """
        将 features 列表解析为结构化字典，供过滤条件和消息格式化使用。

        解析规则
        --------
        features 中每条格式为 "RawKey: Value"，例如 "Type: Studio"。
        RawKey 通过 LISTING_KEY_MAP 映射为标准 key；未知 key 保留小写原样。

        Returns
        -------
        dict，可能包含的 key：
            "type"         → 房型
            "area"         → 面积字符串，含单位，e.g. "26.0 m²"
            "occupancy"    → 入住人数描述
            "floor"        → 楼层，纯数字字符串
            "furnishing"   → 装修类型
            "energy_label" → 能耗标签
            "neighborhood" → 所属片区
            "building"     → 楼盘名

        注意
        ----
        结果在首次调用后缓存于 _feature_map_cache，后续调用直接返回缓存。
        config.py 的 ListingFilter.passes() 和 web.py 的 parse_features
        过滤器都依赖本方法返回的 key 名，修改 LISTING_KEY_MAP 时需同步检查。
        """
        if self._feature_map_cache is None:
            self._feature_map_cache = parse_features_list(self.features)
        return self._feature_map_cache

    def to_dict(self) -> dict:
        """
        序列化为纯 Python dict，供 JSON 输出（--test 模式）使用。

        Returns
        -------
        包含所有字段的 dict，features 保持 list[str] 格式。
        """
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "price_raw": self.price_raw,
            "available_from": self.available_from,
            "features": self.features,
            "url": self.url,
            "city": self.city,
            "sku": self.sku,
            "contract_id": self.contract_id,
            "contract_start_date": self.contract_start_date,
        }
