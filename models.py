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
from typing import Optional

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
    "Offer":        "offer",        # 短租标签，e.g. "Short-stay"
    "Contract":     "contract",     # 合同类型，e.g. "Indefinite" / "6 months max"
    "Tenant":       "tenant",       # 租客要求，e.g. "student only" / "employed only"
    "Address":      "address",      # 街道地址，供 geocode pipeline 用，e.g. "Wenckebachweg 51, 1096 AN Amsterdam"
}


# ------------------------------------------------------------------ #
# 数字解析（模块级，供 config / monitor 复用，避免多处维护正则）
# ------------------------------------------------------------------ #


def parse_float(text: Optional[str]) -> Optional[float]:
    """
    从含单位的字符串中提取浮点数，容忍英文/欧式千分位。

    e.g. "€707" → 707.0, "1,200.50" → 1200.5, "26.0 m²" → 26.0,
         "€ 1.587" → 1587.0, "" → None, None → None
    """
    if not text:
        return None
    m = re.search(r"\d[\d,\.]*", text)
    if not m:
        return None

    token = m.group()
    if "," in token and "." in token:
        # Last separator is the decimal mark; the other separator is thousands.
        if token.rfind(".") > token.rfind(","):
            token = token.replace(",", "")
        else:
            token = token.replace(".", "").replace(",", ".")
    elif "," in token:
        if re.fullmatch(r"\d{1,3}(?:,\d{3})+", token):
            token = token.replace(",", "")
        else:
            token = token.replace(",", ".")
    elif "." in token and re.fullmatch(r"\d{1,3}(?:\.\d{3})+", token):
        token = token.replace(".", "")

    return float(token)


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


#: 同一个概念在上游有多种写法，不归一就会被当成两个不同的值。
#:
#: 2026-08-04 生产实测：**同一个平台**（holland2stay）对同一种合同既返回
#: ``Indefinite``（178 条）又返回 ``Onbepaalde tijd``（38 条）。这不是翻译问题
#: ——翻译只会让两边都变成中文，该合的还是没合。
#:
#: 放在读取层而不是抓取层：抓取层只能修以后的数据，库里已有的 38 条还是错的。
#: 放在 models 而不是某一处调用点：图表要合并计数，筛选下拉要去重，
#: ``ListingFilter.passes`` 要按归一后的值比对——三处必须用同一张表，
#: 否则下拉里只剩 "Indefinite"，匹配时却还在拿原始的 "Onbepaalde tijd" 比，
#: 用户勾了照样收不到。
# Holland2Stay 的 feature 取值有荷兰语和英语两版，同一个属性两种写法都会出现
# （同一批房源里 ``Two (only couples)`` 134 条、``Twee (alleen koppels)`` 47 条）。
# 上游返回哪一版取决于房源录入时的语言，与房源本身无关。
#
# 不归一的后果是筛选按字面匹配：勾了英文那版就收不到荷兰文那版，而下拉里两版
# 并排列着，看起来像两个不同的选项。
#
# 键一律小写（``canonical_feature`` 用 casefold 查表），值取英文写法——面板与
# 通知的默认语言是英文，且 H2S 自己的英文站点用的就是这些词。
#: 标 ✓ 的是 2026-08-05 在生产库里实际出现过的写法；其余是同一维度里尚未见到
#: 荷兰语版本的取值，按荷兰租房的通行说法补上——上游返回哪一版取决于录入语言，
#: 今天只有英文不代表明天不会冒出荷兰文。未出现的条目是惰性的，不会有副作用。
FEATURE_SYNONYMS: dict[str, str] = {
    # ── Contract ──────────────────────────────────────────────────
    "onbepaalde tijd": "Indefinite",              # ✓
    "voor onbepaalde tijd": "Indefinite",
    "maximaal 6 maanden": "6 months max",
    "6 maanden max": "6 months max",
    "maximaal 4 maanden": "4 months max",
    "4 maanden max": "4 months max",
    # ── Occupancy ─────────────────────────────────────────────────
    "eén persoon": "One",                          # ✓
    "een persoon": "One",                          # 上游偶尔丢 accent
    "één persoon": "One",                          # U+00E9 前置组合形式
    "twee personen": "Two",                        # ✓
    "twee (alleen koppels)": "Two (only couples)",  # ✓
    "twee (alleen stellen)": "Two (only couples)",
    "gezin (ouders met kinderen)": "Family (parents with children)",  # ✓
    "drie personen": "Three",
    "vier personen": "Four",
    # ── Type ──────────────────────────────────────────────────────
    # Studio 与数字（1 / 2 / 3 / 4）两种语言写法相同，无需登记。
    "loft (open slaapkamer)": "Loft (open bedroom area)",  # ✓
    # ── Finishing ─────────────────────────────────────────────────
    "gemeubileerd": "Furnished",                   # ✓
    "gemeubeld": "Furnished",
    # Gestoffeerd 直译是「铺了地板窗帘」，荷兰租房语境下即 semi furnished：
    # 有地板、窗帘、灯具，但没有家具。按语境译，不按字面。
    "gestoffeerd": "Semi furnished",               # ✓
    "gedeeltelijk gemeubileerd": "Semi furnished",
    "volledig gemeubileerd": "Fully furnished",
    "ongemeubileerd": "Unfurnished",
    "kaal": "Unfurnished",
    # ── Tenant ────────────────────────────────────────────────────
    "alleen voor studenten": "student only",       # ✓
    "alleen studenten": "student only",
    "alleen voor werkenden": "employed only",
    "alleen werkenden": "employed only",
    "studenten en werkenden": "student and employed",
    "studenten of werkenden": "student and employed",
    # 英文侧的同一个取值。H2S 的 tenant_profile option 6215 原文是「as a student
    # or a working professional」，scrapers/holland2stay.py 照抄成
    # ``student or employed``——语义与上一行的荷兰语版完全相同，只是 and/or
    # 之差。漏登记的后果不是少一个选项，而是**两个都在**：筛选下拉从库里取
    # distinct 值（mstorage._listings.get_feature_values），归一不掉就会并排
    # 出现「学生/上班族」和一个没翻译的 student or employed，勾了前者的用户
    # 静默收不到后者那批房源。2026-08-25 生产库里正好有 16 条。
    "student or employed": "student and employed",
    # ── Offer ─────────────────────────────────────────────────────
    "kort verblijf": "Short-stay",
    "short stay": "Short-stay",                    # 英文侧的连字符写法差异
    "parkeerplaats inbegrepen": "Parking included",
    "parkeren inbegrepen": "Parking included",
    # 「Receive €150 cash back」这类带金额的优惠没有登记：金额是变量，静态表
    # 覆盖不全，硬编几个常见值反而给人「已经覆盖」的错觉。目前生产库里只出现
    # 过英文版。
}


def canonical_feature(value: str) -> str:
    """把上游 feature 取值归一到规范写法；未收录的原样返回。"""
    return FEATURE_SYNONYMS.get(str(value).strip().casefold(), str(value).strip())


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
    source          房源所在的第三方平台标识，与 ``scrapers.SCRAPER_REGISTRY`` 的
                    key 一致。P0 阶段默认 ``"holland2stay"``，单源行为不变；
                    P1 起新平台（OurDomain / DUWO 等）会用 ``"ourdomain"`` / ``"duwo"``。
                    UI / 通知模板可据此显示 source badge 区分平台来源。
                    **id 字段在 P0 仍是 H2S 的 url_key 原样**，未做前缀化；
                    跨平台 id 唯一性的迁移留到 P1 接 OurDomain 时一起做，
                    避免提前重写 status_changes / web_notifications / iOS deep link。
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
    source: str = "holland2stay"

    #: 这一轮的 ``status`` 没能通过平台的权威校验（**不是**「状态未知」，而是
    #: 「只有上游 feed 说了算，而那个 feed 已知会滞后」）。
    #:
    #: 谁会设：目前只有 Xior——它的 WP feed 会把已经订走的单元继续挂着，所以
    #: 每轮要另外抓一次 ``floorplans.aspx`` 求权威可订集合；那个页面在
    #: Cloudflare 后面，2026-08-25 实测约 29% 的轮次拿不到。
    #:
    #: 谁会读：``mstorage._listings.diff()``。拿不到校验时**不许把一条已知不可
    #: 订的房源翻成可订**——见 ``_should_hold_unverified``。这一位只描述「这次
    #: 报的可订有没有依据」，怎么处置由存储层决定，scraper 不需要知道旧状态。
    #:
    #: 不参与 ``__eq__``：它是这一轮的取数元信息，不是房源快照的一部分。两条
    #: 描述同一套房子的 Listing 不该因为「这轮校验通没通」而判为不等。
    status_unverified: bool = field(default=False, compare=False)

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
            "source": self.source,
        }
