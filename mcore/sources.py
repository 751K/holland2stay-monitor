"""平台能力的单一注册表：SourceSpec。

以前同一份平台差异散在四个平行集合里（浏览器型、可无代理、可自动预订、
REST 唤醒），加平台时要记得逐个补。现在每个 source 只声明一次，集合由
注册表派生。

注意：``AUTO_BOOK_SOURCES`` 保持元组顺序，因为用户看到/日志里的顺序曾经
就是 ("holland2stay", "plaza")。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class SourceSpec:
    name: str
    #: 需要 Playwright 浏览器传输（必须绑专属长存线程）。
    browser_transport: bool = False
    #: 代理全挂、降级直连时还能抓。
    proxyless_capable: bool = False
    #: 自动预订流程已端到端验证通过。
    auto_bookable: bool = False
    #: REST 触发器发现新房时，这个 source 值得单独叫醒一轮。
    rest_wake: bool = False


SPECS: dict[str, SourceSpec] = {
    # auto_bookable 的进组条件是「用真实账号端到端跑通过」：
    #   holland2stay  2026-05-22 真实下单成功（见 ARCHITECTURE §7）
    #   plaza         2026-09-10 真实应征成功（见 docs/PLAZA.md §6）
    #
    # Plaza 另有一道**用户侧开关** auto_book.plaza_enabled，默认关——进了这个
    # 元组不等于对谁都开。理由见 can_auto_book 里的注释。
    #
    # **Xior 先不放进来**：bookers/rentcafe.py 里第 3 步之后的多步表单是没走过
    # 流程硬猜出来的草稿，放开等于拿用户的真实账号去提交半懂不懂的表单。等流程
    # 侦察完并验证过再加。凭据判定（can_auto_book）已经就位，到时只改这里。
    "holland2stay": SourceSpec(
        "holland2stay",
        browser_transport=True,
        auto_bookable=True,
        rest_wake=True,
    ),
    "xior": SourceSpec("xior", browser_transport=True),
    "ourdomain": SourceSpec("ourdomain", proxyless_capable=True),
    "ourcampus": SourceSpec("ourcampus", proxyless_capable=True),
    "plaza": SourceSpec("plaza", auto_bookable=True),
    "magis": SourceSpec("magis"),
    "studentexperience": SourceSpec("studentexperience"),
}


def _names(field: str) -> frozenset[str]:
    return frozenset(name for name, spec in SPECS.items() if getattr(spec, field))


BROWSER_SOURCES = _names("browser_transport")
PROXYLESS_CAPABLE_SOURCES = _names("proxyless_capable")
REST_WAKE_SOURCES = _names("rest_wake")

#: 元组顺序保留历史顺序（H2S 在前）。
AUTO_BOOK_SOURCES: tuple[str, ...] = tuple(
    name for name, spec in SPECS.items() if spec.auto_bookable
)


# ── 平台注册表与显示名 ──────────────────────────────────────────────

#: 系统支持的全部平台。**新增平台只改这里。**
#:
#: 这个常量是补出来的：原先每处各自维护一份平台清单，于是 ``ourcampus``
#: 被漏了三次——前端的 sourceLabel（三份实现都没有它）、monitoring 页（干脆
#: 不转换）、以及全局设置的白名单（从面板保存一次就会把它从 SOURCES 里悄悄
#: 删掉）。漏的都是同一个平台，因为它是最后加的那个。
KNOWN_SOURCES: tuple[str, ...] = ("holland2stay", "ourdomain", "ourcampus",
                                  "xior", "magis", "studentexperience", "plaza")

#: 平台显示名。前端另有一份同样的表（static/app.js 的 SOURCE_LABELS），
#: 因为那边是客户端渲染；两边都从这份注释里的同一个事实来。
SOURCE_DISPLAY_NAMES: dict[str, str] = {
    "holland2stay": "Holland2Stay",
    "ourdomain": "OurDomain",
    "ourcampus": "OurCampus",
    "xior": "Xior",
    "magis": "Magis",
    "studentexperience": "Student Experience",
    "plaza": "Plaza",
}


def source_display_name(source: str) -> str:
    """平台显示名；未知 key 首字母大写后返回，不套用某个已知平台名。"""
    key = (source or "").strip().lower()
    return SOURCE_DISPLAY_NAMES.get(key, key.capitalize() if key else "")


# ── 过滤维度能力表 ──────────────────────────────────────────────────

_UNIVERSAL_FILTER_DIMS = frozenset({
    "max_rent", "min_area", "city", "source", "building"})
_SOURCE_FILTER_DIMS: dict[str, frozenset] = {
    # tenant 曾在 2026-08-18 被摘出此表：H2S 上线 operation 白名单后我们只能照抄
    # 它那条 GetCategories，而租客属性不在该查询的字段集里，房源因此不带 Tenant
    # 标签；而这个维度是 fail-closed 的，缺值即拒绝——留着注册会让勾「仅学生」
    # 的用户一条 H2S 房源都收不到。
    #
    # 2026-08-19 恢复：站点另有一条同样在白名单里的 GetProductDetail，字段集里有
    # tenant_profile，按需单取即可补齐（scrapers/holland2stay.py 的详情补齐）。
    # 取值语义从站点详情页正文逐条实测确定，见 _TENANT_PROFILE_LABELS。
    #
    # ⚠️ 补齐是**按预算分摊**的（每轮上限 20 条），冷启动后要若干轮才铺满。
    # 期间未补到的房源没有 Tenant 标签，会被 fail-closed 拒掉——这是有意的取舍：
    # 宁可少推几条，不可把「不确定能不能租」的房源推给勾了资格限制的用户。
    "holland2stay": _UNIVERSAL_FILTER_DIMS | {
        "floor", "occupancy", "type", "neighborhood",
        "contract", "offer", "finishing", "energy", "tenant",
    },
    # finishing 不是抓来的，是 SOURCE_ASSUMED_FEATURES 声明的整栋楼事实。
    # 登记进来之后该维度不再走 fail-open：勾 Unfurnished 时这些房源会被正确排除，
    # 而此前 fail-open 会把它们一并放行——它们恰恰不是无家具的。
    # tenant 同理，不是抓来的：判据只存在于平台官网的 Criteria 页，由
    # scrapers/ourdomain.py 的 tenant_policy 声明（见 docs/OURDOMAIN.md §5.1）。
    # **必须登记**，否则 allowed_tenant 对本平台整体跳过——用户勾了「仅学生」
    # 依然会收到 Young-Professionals-only 的房源，这个维度等于没做。
    # 代价是 Diemen 偶发拿不到面积的单元（此时不写 Tenant）会在筛选时被排除；
    # 与 floor 一样取 fail-closed，宁可少推不可错推。
    "ourdomain": _UNIVERSAL_FILTER_DIMS | {
        "floor", "occupancy", "type", "finishing", "tenant",
    },
    # OurCampus 复用 OurDomain 的解析器，抓得到的维度完全相同；但它至今没返回过
    # 任何单元，装修档位无从核实，所以不登记 finishing。
    # tenant 不同：它不依赖单元数据，整栋恒为 student only（官网 criteria 要求
    # 在校证明，PhD 与博后明确不符合），所以可以登记。
    "ourcampus": _UNIVERSAL_FILTER_DIMS | {"floor", "occupancy", "type", "tenant"},
    # xior 的 tenant 来自 SOURCE_ASSUMED_FEATURES，每条房源都带，可安全登记。
    "xior": _UNIVERSAL_FILTER_DIMS | {"finishing", "tenant"},
    # magis 的卡片直接给出楼层、户型、装修档位与能耗标签，12/12 条实测都能解析出，
    # 因此这四个维度都登记。
    #
    # tenant 不登记：站点只在部分房源上打「Students only」徽标（12 条里 3 条），
    # 而「没有徽标」是「不限」还是「未标注」没有证据。这个维度 fail-closed，
    # 登记之后另外 9 条会被勾了租客条件的用户整体过滤掉。徽标本身仍写进 features，
    # 通知里看得见，只是不参与筛选。见 scrapers/magis.py 的 TENANT_MAP。
    "magis": _UNIVERSAL_FILTER_DIMS | {"floor", "type", "finishing", "energy"},
    # Student Experience 是户型级而非单元级：一条 listing 对应一个户型（多间），
    # 面积因此是区间。登记的维度只有 type 与 tenant——
    #
    #   type    站点全部产品都是 studio，四个档位名（Signature / Essential /
    #           Prestige / Comfort）是价位分层不是房型，统一写 Type: Studio。
    #   tenant  来自 SOURCE_ASSUMED_FEATURES，整个 source 恒为 student only
    #           （FAQ：exclusively available for students，签约需在读证明）。
    #
    # finishing **不登记**：规格行只出现在主卡片上，「其它户型」滑块里的紧凑卡片
    # 没有这一行。该维度 fail-closed，登记之后紧凑卡片那几条会被勾了装修档位的
    # 用户整体过滤掉——与 magis 的 tenant 同一取舍。值仍写进 features
    # （Finishing note），通知里看得见，只是不参与筛选。
    #
    # floor / energy 站点不给。楼层只在设施行的散文里出现过（"located on
    # floor 5-8"），那是户型的整体描述而非某一间的楼层。
    "studentexperience": _UNIVERSAL_FILTER_DIMS | {"type", "tenant"},
    # Plaza 的 feed 是结构化 JSON，房型、楼层、租客都是主键字段而不是散文，因此
    # 三个都登记。
    #
    # tenant 与 Xior / OurCampus / Student Experience 不同：**不是**整站断言，是
    # 每条房源自己的 doelgroepen（2026-09-02 快照 student 39 / regulier 16 并存）。
    # 只有 doelgroepen 恰好等于 {student} 的才写 student only——混合标记的房源
    # 不写，让该维度对它们 fail-open，而不是替站点断言「学生也能租」。
    #
    # energy 不登记：energyLabel 53 条全是空对象。
    # finishing / occupancy / contract 站点不给（typeContract 53 条全为 null）。
    "plaza": _UNIVERSAL_FILTER_DIMS | {"type", "floor", "tenant"},
}


#: 取值互斥、必须整体相等才算命中的维度。
#:
#: finishing 的四个取值是四档装修程度：Unfurnished / Semi furnished /
#: Furnished / Fully furnished。它们在下拉里各占一项，选哪一档就是哪一档——
#: 「半装修」不算「有家具」，「全装修」也不该被「有家具」顺带收走。想要多档
#: 就多勾几项，这是白名单本来的用法。
#:
#: 其余维度不能这么做：房型在 H2S 写 ``1``、在 OurDomain 写
#: ``1-Bedroom Apartment``，要求整体相等会让跨平台的同一户型对不上。


def sources_supporting_dim(dim: str) -> list[str]:
    """哪些平台会对这个维度真正生效，按 KNOWN_SOURCES 的顺序返回。

    给界面用。平台不支持某维度时该条件对它整体跳过（fail-open，见上方注释），
    这个行为本身是对的——否则一套条件会把整批抓不到该属性的房源误杀——但界面
    上此前一个字都没提：勾了「能耗 ≥ A」的用户会以为收到的都是 A 级，实际
    Xior 的房源一条都没过能耗这一关。
    """
    return [s for s in KNOWN_SOURCES if dim in _SOURCE_FILTER_DIMS.get(s, _UNIVERSAL_FILTER_DIMS)]


SOURCE_ASSUMED_FEATURES: dict[str, dict[str, str]] = {
    # Xior 是纯学生盘：品牌名就是 Xior Student Housing，官网通篇「Trusted by
    # 22,000+ students for their university journey」，全站没有任何收入或雇佣
    # 条款。整个 source 一个值，不需要按楼或按面积切——所以放这里，而不是像
    # OurDomain 那样在 BUILDINGS 里配 tenant_policy（那边 Diemen 一栋楼内部
    # 就分两档，非切不可）。
    #
    # ⚠️ Finishing 曾经也在这里写着 "Furnished"，2026-08-21 实测发现是错的：
    # 装修档位**按 room type 变**，同一栋楼内部就分两档。
    #
    #     Amsterdam Naritaweg   Deluxe / Unfurnished   站点无任何 furnished 项
    #     Amsterdam Naritaweg   Deluxe / Furnished     Fully furnished
    #     Aachen Vaals          Comfy                  Partially furnished
    #                                                  （床架有、床垫没有）
    #     Amsterdam Karspeldreef / Eindhoven ×2 /
    #     Groningen / Utrecht                          Fully furnished
    #
    # 生产库 69 条里 41 条被标错（3 条实为无家具、38 条实为半装）。方向是**错推**：
    # 勾 Furnished 的用户收到它们，勾 Unfurnished / Semi furnished 的反而收不到。
    #
    # 现改由 scrapers/xior.py 的 BUILDING_FURNISHING 按楼登记 + 按 floorplan
    # 名判定，验证过一栋登记一栋；没登记的不写该字段。
    "xior": {"Tenant": "student only"},
    "ourdomain": {"Finishing": "Furnished"},
    # Student Experience 同为纯学生盘，且判据比 Xior 更硬——不是从品牌宣传推断的，
    # 是 FAQ 的明文规则：「All Student Experience studios are exclusively
    # available for students」，签约前必须通过服务门户上传在读证明，入住时必须
    # 是在校注册状态。合格身份含 study programme / internship / PhD research。
    #
    # ⚠️ 与 OurCampus 相反：那边的 criteria 明确排除 PhD 与博后。两处都写了
    # student only，但含义不同——本平台收 PhD，OurCampus 不收。真出现按学位分档
    # 的筛选需求时，这两个值不能合并处理。
    #
    # Finishing 不放这里：它按户型变（Signature 有规格行、Essential 的紧凑卡片
    # 没有），整个 source 一刀切正是 Xior 2026-08-21 栽过的那个跟头。
    "studentexperience": {"Tenant": "student only"},
}


def assumed_features(source: str) -> list[str]:
    """该平台恒定成立的 feature 条目，形如 ``["Finishing: Furnished"]``。"""
    return [f"{k}: {v}" for k, v in SOURCE_ASSUMED_FEATURES.get(source, {}).items()]


def source_supports_dim(source: Optional[str], dim: str) -> bool:
    """公开入口，供浏览页复用同一套 fail-open 判定。

    浏览页此前没有这一层，一条没有该属性的房源必然不匹配，等于因为上游少给一个
    字段就把整个平台从结果里抹掉。
    """
    return _source_supports_dim(source, dim)


def filter_dim_sources() -> dict[str, list[str]]:
    """每个过滤维度实际生效于哪些平台，形如 ``{"contract": ["holland2stay"]}``。

    为什么要把这张表送到客户端
    --------------------------
    ``_SOURCE_FILTER_DIMS`` 决定的是**沉默的**行为：用户在筛选表单里勾了
    "Contract: Indefinite"，对 Holland2Stay 生效，对其余六个平台整条跳过
    （fail-open，见 ``_source_supports_dim``）。界面上没有任何地方说过这件事，
    用户会以为自己设了一条全局条件——他设的其实是一条单平台条件。

    这张表是 ``_SOURCE_FILTER_DIMS`` 的转置，不是另抄一份：新增平台或调整维度
    只改上面那张表，这里跟着变。

    未登记的平台按 ``_UNIVERSAL_FILTER_DIMS`` 处理，与 ``_source_supports_dim``
    的回退一致——它们不出现在专有维度的列表里。
    """
    dims: dict[str, list[str]] = {}
    for source, caps in _SOURCE_FILTER_DIMS.items():
        for dim in caps:
            dims.setdefault(dim, []).append(source)
    for dim in dims:
        dims[dim].sort()
    return dims


def _source_supports_dim(source: Optional[str], dim: str) -> bool:
    """该平台是否稳定提供某过滤维度。

    未登记的平台默认只认通用维度（price/area/city/source），其余平台专有维度
    一律跳过——多平台场景下宁可放行也别误杀；新平台需要更严的过滤时在
    ``_SOURCE_FILTER_DIMS`` 里登记。
    """
    caps = _SOURCE_FILTER_DIMS.get((source or "holland2stay").strip().lower(),
                                   _UNIVERSAL_FILTER_DIMS)
    return dim in caps
