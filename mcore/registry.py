"""平台的静态事实：各平台的城市 / 楼栋注册表、楼盘名→城市归一、Xior 楼栋装修档位。

从 config.py 拆出。这里只放**数据和由数据派生的纯函数**，只依赖标准库，
所以 config、target_config、scrapers、mstorage 都可以直接从这里取，不再为了
一张城市表绕回 config（以前 config ↔ target_config / scrapers 的环就是这么来的）。
config 保留这些名字的 re-export。

非 H2S 平台的「配置键 / 注册表 / 留空含义」统一登记在 :data:`TARGET_SPECS`，
config 的任务展开、target_config 的校验、设置页的回显都从它派生。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


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


@dataclass(frozen=True)
class TargetSpec:
    """一个按楼盘 / 城市订阅的平台（H2S 之外）的配置约定。"""
    source: str
    #: .env 里的订阅串键名，格式 ``显示名,key|显示名,key``。
    env_key: str
    #: 官方注册表：每项至少有 ``key``，显示名取 :func:`target_name`。
    registry: list[dict]
    #: ``Config`` 上存这个平台订阅结果的字段名。
    config_field: str
    #: 键**不存在**时用的订阅串。
    default_raw: str = ""
    #: 留空是否等于「注册表全选」。为 False 时留空就是一个都不抓。
    #:
    #: 一次请求就拿到全站的平台（Magis / Student Experience / Plaza）多订几个城市
    #: 不产生额外请求，留空全收是合理默认；Xior 是历史约定。
    all_when_empty: bool = False


TARGET_SPECS: dict[str, TargetSpec] = {
    "ourdomain": TargetSpec(
        "ourdomain", "OURDOMAIN_CITIES", KNOWN_OURDOMAIN_CITIES, "ourdomain_cities",
        default_raw="Amsterdam Diemen,diemen"),
    "ourcampus": TargetSpec(
        "ourcampus", "OURCAMPUS_CITIES", KNOWN_OURCAMPUS_CITIES, "ourcampus_cities",
        default_raw="OurCampus Amsterdam Diemen,diemen"),
    "xior": TargetSpec(
        "xior", "XIOR_CITIES", KNOWN_XIOR_CITIES, "xior_cities",
        all_when_empty=True),
    "magis": TargetSpec(
        "magis", "MAGIS_CITIES", KNOWN_MAGIS_CITIES, "magis_cities",
        all_when_empty=True),
    "studentexperience": TargetSpec(
        "studentexperience", "STUDENTEXPERIENCE_CITIES",
        KNOWN_STUDENTEXPERIENCE_CITIES, "studentexperience_cities",
        all_when_empty=True),
    "plaza": TargetSpec(
        "plaza", "PLAZA_CITIES", KNOWN_PLAZA_CITIES, "plaza_cities",
        all_when_empty=True),
}


def target_name(entry: dict) -> str:
    """注册表条目的显示名。Xior 的条目没有 ``name``，由「城市 楼栋」拼出。"""
    return entry.get("name") or f"{entry.get('city', '').strip()} {entry.get('bldg', '').strip()}".strip()


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
    for spec in TARGET_SPECS.values():
        if spec.source in enabled:
            names.update(c.get("city") or c["name"] for c in spec.registry)
    names.discard("")
    return sorted(names)


# ── 装修档位 ────────────────────────────────────────────────────────

#: Finishing 的四档取值，与 models.py 术语表的归一结果一致。
FIN_FULLY = "Fully furnished"
FIN_FURNISHED = "Furnished"
FIN_SEMI = "Semi furnished"
FIN_UNFURNISHED = "Unfurnished"

#: ``{building_key: 档位}``。每一条都对着站点房型页核过，注释写明依据。
#: 新增楼栋**不要凭印象填**——核不了就别登记，留空比填错好。
XIOR_BUILDING_FURNISHING: dict[str, str] = {
    # 2026-08-21 逐栋核对（站点房型卡片的 FEATURES 清单）
    "p0196062": FIN_FULLY,   # Amsterdam Karspeldreef —— 3 个房型全是 Fully furnished
    "p0196467": FIN_FULLY,   # Eindhoven Kronehoefstraat —— 3 个房型全是 Fully furnished
    "p0195855": FIN_FULLY,   # Eindhoven Zernikestraat —— Comfy / Deluxe 均 Fully furnished
    "p0196098": FIN_FULLY,   # Groningen Eendrachtskade —— Comfy / Deluxe 均 Fully furnished
    "p0196503": FIN_FULLY,   # Utrecht Willem Dreeslaan —— 仅 Essential，Fully furnished
    # Aachen Vaals Katzensprung —— 站点写的是 "Partially furnished"：床架有、
    # **床垫没有**（"Bed frame - no mattress"），无 Wardrobe。按 models.py 的
    # 术语表这是 Semi furnished（荷兰语 gestoffeerd），不是 Furnished。
    "p0196061": FIN_SEMI,
    # Amsterdam Naritaweg 不登记：它四个房型分 Furnished / Unfurnished 两档，
    # 由下面的房型名规则逐条判定，登记一个整栋值反而会盖掉正确答案。
}

#: floorplan 名里的档位后缀。Xior 只在同一栋楼同时卖两种时才加后缀。
_FP_UNFURNISHED = re.compile(r"\bunfurnished\b", re.IGNORECASE)
_FP_FURNISHED = re.compile(r"\bfurnished\b", re.IGNORECASE)


def xior_furnishing_for(building_key: str, floorplan_name: str) -> Optional[str]:
    """该单元的装修档位；判不出返回 None（调用方不写该字段）。

    房型名优先于楼栋登记：后缀是上游针对**这一个房型**的声明，比整栋的概括更准。
    """
    name = floorplan_name or ""
    if _FP_UNFURNISHED.search(name):
        return FIN_UNFURNISHED
    if _FP_FURNISHED.search(name):
        # 站点上带 "/ Furnished" 后缀的房型，FEATURES 里写的都是 Fully furnished
        return FIN_FULLY
    return XIOR_BUILDING_FURNISHING.get(building_key)
