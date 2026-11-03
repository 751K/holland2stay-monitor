"""filters.py — 用户级过滤与自动预订配置
=========================================
``ListingFilter``（房源是否推给这个用户 / 是否触发自动预订）和
``AutoBookConfig``（单个用户的自动预订设置），以及它们用到的匹配规则。

从 config.py 拆出：这些是每个用户存在 SQLite ``user_configs`` 里的领域对象，
和进程级的 ``Config`` 不是一回事。本模块只依赖 models 与 mcore 的注册表，
**不读环境变量、不加载 .env**——只想用 ``ListingFilter`` 的模块不必再为此
触发 config 的启动引导。config 保留这些名字的 re-export。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Optional

from mcore.registry import canonical_city
from mcore.sources import _source_supports_dim
from models import canonical_feature, parse_float, parse_int

if TYPE_CHECKING:
    from bookers.applicant_profile import ApplicantProfile
    from models import Listing

logger = logging.getLogger(__name__)


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
    # 方式（见 docs/PLAZA.md §3.4）。资料在**注册**阶段一次性交给站点（€27.50/年），
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
