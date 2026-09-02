"""
scrapers/plaza.py — Plaza (newnewnew.space) scraper
====================================================

Plaza（plaza.newnewnew.space，运营方 Plaza Resident Services）是跑在 **Zig/Hexia**
平台上的租房门户，站点自我描述是「Woonruimte voor studenten, starters en expats」。
覆盖荷兰多城，另有德国 Bochum 与波兰 Poznan——本 scraper **只取荷兰**。

传输
----
纯 HTTP，**一个 POST 拿全站**，无认证、无 Cloudflare、无 JS 挑战：

    POST /portal/object/frontend/getallobjects/format/json

空 body 即可（GET 也返回同样内容）。2026-09-02 实测 167 KB / 55 条。
``robots.txt`` 只禁 ``/portal/uploads/*/floorplans/*`` 与 ``/portal/uploads/*.pdf``，
本端点不在禁用范围内——**因此 floorplans 里的文件不抓**，只用 pictures。

为什么接：当前在架的两种模型都奖励速度
--------------------------------------
2026-09-02 快照里荷兰住宅 49 条（全部住宅 53 条，其中 4 条在德国），只用到两种
分配模型，**判据取自站点自己的 ``gettranslations``，不是从字段名直译**：

``dth``（31 条，``model.advertentieSluitenNaEersteReactie`` 为真）
    站点内部叫 DTH，标题 "Eerste reactie"。应征即**当场成交**——确认框原话：
    「Wil je deze kamer definitief boeken? Dat betekent dat je deze kamer accepteert
    en **geen andere aanbieding meer krijgt**。」这是目前所有已接与已侦察平台里
    最强的形态：不是「抢排位」，是点下去房子就是你的。代价也最重（放弃其它
    offer），所以那句话原样写进 features。
    这 31 条全部是 Utrecht 的学生 studio（16 m²，€712.75–€867.75）。

``reactiedatum``（18 条）
    站点给用户看的标签是「**Snelle reageerder**」（快速响应者），说明是「Wij
    werken **niet met een wachtlijst**…Voldoe jij daarmee aan alle criteria dan
    maak je **meteen** kans op de woning」。有 ``closingDate`` 兜底，但**不是**
    「到点统一开奖」——快仍然是优势。

⚠️ 平台**支持但 Plaza 当前一条都没用**的模型：``loting``（截止后电脑抽签）、
``inschrijfduur``（按注册时长排队）、``hospiteren``（合租面试）、``woningruil``。
**这几类推送没有价值**——早知道不提高中签概率，与 Vesteda 被否是同一个理由（见
``docs/SCRAPING_RECON.md`` §6b）。将来它们可能出现，因此模型是**逐条记录**的，
不是整站断言；``ALLOCATION_LABELS`` 里给全了六种的文案。

与 DUWO/ROOM 的区别（这一条决定了能不能接）
--------------------------------------------
两者都要花钱注册：ROOM 约 €30/年，Plaza 是 €27.50/年。DUWO 因此被否（见
``docs/SCRAPING_RECON.md`` §6），但**否决理由不是收费本身**：

- ROOM 的 ``product-search`` 对匿名请求返回 404，房源**只有登录后才看得见**。要抓
  就得拿一个真实账号维持 session，再把登录后的内容转发给非账户持有者——其 ToS
  明确禁止「将通过本服务获得的信息再行分发」。
- Plaza 的房源是**公开的**：上面那个端点无 cookie、无 referer 直接 200（2026-09-02
  两次独立验证）。站点 disclaimer 只有标准免责声明，没有再分发限制条款。

也就是说 €27.50 买的是「能应征」，不是「能看见」——与 Xior / H2S 需要账号才能下单
是同一类，不影响只读监控。**但这笔钱要让用户知道**：所有房源的
``inschrijvingVereistVoorReageren`` 都是 true，收到通知却没注册就没法应征。因此
每条 listing 都写一条 ``Registration`` 特征。

城市清单会漂
------------
``KNOWN_PLAZA_CITIES`` 是 2026-09-02 的快照：站点导航自述的八个城市
（Maastricht / Amsterdam / Utrecht / Eindhoven / Enschede / Delft / Breda /
Arnhem）与当时实际在架的十个城市取并集。两份清单本来就对不上——在架的 Geldrop
（6 条，当时第二多）、Groot-Ammers、Deventer、Duivendrecht 都不在导航里。

所以这个清单**一定会漂**。未登记城市的房源不会被静默丢掉：``scrape()`` 里按
WARNING 记下城市名，日志里看得见，加进表即可。宁可漏推几条也不要猜城市——猜错
会把房源分派给错误的 ScrapeTask，用户按城市订阅就会收到不该收的。

价格口径
--------
``totalRent`` 是到手价（实测 €867.75），``netRent`` 是 kale huur（€653）。
``price_raw`` 报前者，与 H2S / Xior / Magis 同一口径；后者写进 features 的
``Net rent`` 供核对。

维度登记
--------
- ``type``：站点取值 Studio / Appartement / Vrijstaande woning，归一到英文写入
  （``canonical_feature`` 不做荷英映射，写荷兰语会与其余平台的词表对不上）。
- ``floor``：``floor.localizedName`` 是荷兰语序数——``Begane grond`` = 0、
  ``Ne verdieping`` = N。认不出就不写。
- ``tenant``：来自每条房源自己的 ``doelgroepen``，**不是** ``SOURCE_ASSUMED_FEATURES``
  的整站断言。这比 Xior / OurCampus 那种一刀切可靠——同一批里 student 与 regulier
  并存（2026-09-02：39 / 16）。只有明确标 student 的才写 ``student only``。
- ``energy`` **不登记**：``energyLabel`` 53 条全是 ``{"icon": null, "id": null}``。
- ``finishing`` / ``occupancy`` / ``contract`` 站点不给（``typeContract`` 53 条全
  为 null）。

不做自动预订
------------
应征需要付费账号，且流程未侦察、ToS 暴露面未评估。与 OurCampus / Magis /
Student Experience 一致：只通知，不预订。
"""
from __future__ import annotations

import json
import logging
import re
from contextlib import contextmanager
from typing import Optional

from models import Listing

from .base import AbstractScraper, ScrapeNetworkError, ScrapeResult, ScrapeTask

logger = logging.getLogger(__name__)


BASE_URL = "https://plaza.newnewnew.space"
LIST_URL = f"{BASE_URL}/portal/object/frontend/getallobjects/format/json"
DETAIL_PATH = "/aanbod/huurwoningen/details/"

#: 荷兰的 ``land.id``。站点还有德国（``268``，Bochum）与波兰的房源。
#:
#: 用 ``land.id`` 而不是 ``regio.name`` 的前缀判断：后者是展示文案
#: （"Nederland - Utrecht"），改文案就会静默失准；前者是主键。两者 2026-09-02
#: 一致，可互为交叉校验。
NL_LAND_ID = "524"

#: 只要住宅。同一个端点还返回停车位与储藏间（``voorVoertuig``），站点自己的
#: 房源页用 ``dwellingType.categorie = woning`` 这个隐藏筛选把它们排掉。
HOUSING_CATEGORY = "woning"


#: 荷兰城市清单（2026-09-02 快照，见模块文档「城市清单会漂」）。
CITIES: tuple[str, ...] = (
    "Amsterdam", "Arnhem", "Breda", "Delft", "Deventer", "Duivendrecht",
    "Eindhoven", "Enschede", "Geldrop", "Groot-Ammers", "Maastricht", "Utrecht",
)

#: 站点房型 → 本项目词表。归一到英文，理由见模块文档「维度登记」。
TYPE_MAP = {
    "studio": "Studio",
    "appartement": "Apartment",
    "vrijstaande woning": "House",
    "eengezinswoning": "House",
    "kamer": "Room",
}

#: ``doelgroepen`` 里代表「仅学生」的 code。
_STUDENT_CODE = "student"

#: 分配模型 → 给用户看的一句话。**文案依据是站点自己的 ``gettranslations``**，
#: 不是从字段名直译。
#:
#:   dth            ``model.advertentieSluitenNaEersteReactie`` 为真。站点内部叫
#:                  DTH，标题 ``ModelTitleDTH`` = "Eerste reactie"，说明
#:                  ``VolgordeBepalingDescriptionDTH`` = 「De eerste die reageert
#:                  en voldoet aan de voorwaarden…, krijgt de woning aangeboden」。
#:                  应征时会弹确认框（``bevestigAdvertentieSluitenNaEersteReactie``）：
#:                  「Wil je deze kamer definitief boeken? Dat betekent dat je deze
#:                  kamer accepteert en **geen andere aanbieding meer krijgt**。」
#:                  ——所以这不只是「抢得快」，是**当场成交并放弃其它 offer**，
#:                  代价必须写出来。
#:   reactiedatum   ``reactiedatumfilteroptionlabel`` = "Snelle reageerder"，
#:                  说明「Wij werken **niet met een wachtlijst**…Voldoe jij daarmee
#:                  aan alle criteria dan maak je **meteen** kans op de woning」。
#:                  有截止时间，但**不是**「到点统一开奖」——快仍然是优势。
#:
#: 平台还支持 loting（电脑抽签）、inschrijfduur（按注册时长排队）、hospiteren
#: （合租面试）、woningruil（换房）——**这几类推送没有价值**（早知道不提高中签
#: 概率，与 Vesteda 被否的理由同类，见 docs/SCRAPING_RECON.md §6b）。2026-09-02
#: 快照里 Plaza 一条都没有，但将来可能出现，因此逐条记录而不是整站断言。
ALLOCATION_LABELS = {
    "dth": "direct booking — first valid response books it outright "
           "(accepting forfeits other offers)",
    "reactiedatum": "fast responder — no waiting list, speed matters",
    "loting": "lottery — drawn after the deadline, speed does not help",
    "inschrijfduur": "queue — ranked by registration duration, speed does not help",
    "hospiteren": "hospiteren — current residents pick the new housemate",
    "woningruil": "home swap",
}

_RE_FLOOR = re.compile(r"^(\d+)e\s+verdieping$", re.I)
_RE_GROUND = re.compile(r"^begane\s+grond$", re.I)


def _floor(label: str) -> str:
    """``Begane grond`` → ``"0"``；``3e verdieping`` → ``"3"``；认不出返回空串。"""
    label = (label or "").strip()
    if _RE_GROUND.match(label):
        return "0"
    m = _RE_FLOOR.match(label)
    return m.group(1) if m else ""


def _fmt_euro(value: float) -> str:
    """``867.75`` → ``"€867,75"``；整数不带小数。

    站点自己按荷兰习惯用逗号做小数点，跟着它走。
    """
    if float(value).is_integer():
        return f"€{int(value)}"
    return f"€{value:.2f}".replace(".", ",")


def _name_of(node: object) -> str:
    """从 ``{"name": …}`` / ``{"localizedName": …}`` 这类节点取名字。"""
    if not isinstance(node, dict):
        return ""
    for key in ("localizedName", "name", "localizedNaam"):
        v = node.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _parse_object(obj: dict) -> Optional[Listing]:
    """一条 API 记录 → Listing；不是荷兰住宅、或缺关键字段时返回 None。

    「关键」是 id、城市与价格：没有 id 无法去重，没有城市无法分派，没有价格会被
    所有带租金上限的筛选整体漏掉。
    """
    if not isinstance(obj, dict):
        return None

    dwelling = obj.get("dwellingType") or {}
    if dwelling.get("categorie") != HOUSING_CATEGORY:
        return None
    if str((obj.get("land") or {}).get("id") or "") != NL_LAND_ID:
        return None
    if obj.get("isGepubliceerd") is False:
        return None

    oid = obj.get("id")
    city = _name_of(obj.get("city"))
    rent = obj.get("totalRent")
    if oid in (None, "") or not city or rent in (None, "", 0):
        return None
    try:
        rent = float(rent)
    except (TypeError, ValueError):
        return None

    features: list[str] = []

    def _add(key: str, value: object) -> None:
        if value not in (None, "", []):
            features.append(f"{key}: {value}")

    street = (obj.get("street") or "").strip()
    number = str(obj.get("houseNumber") or "").strip()
    addition = (obj.get("houseNumberAddition") or "").strip()
    address = " ".join(p for p in (street, number, addition) if p)

    model = obj.get("model") or {}
    code = (model.get("modelCategorie") or {}).get("code") or ""
    # 分配模型写进 features 而不是塞进 status——它不是可用性，是「怎么分配」。
    # 用户看得见才能判断该多快动手，以及**动手的代价**。
    #
    # 文案取自站点自己的 gettranslations，不是从字段名猜的（2026-09-02 曾经就是
    # 从字段名直译，把 DTH 说成「回应后广告关闭、再从回应者里挑」，漏掉了它其实
    # 是当场成交、且要放弃其它 offer）。
    _add("Allocation", ALLOCATION_LABELS.get(
        "dth" if model.get("advertentieSluitenNaEersteReactie") else code, code))

    _add("Type", TYPE_MAP.get(_name_of(dwelling).lower(), ""))
    _add("Dwelling type", _name_of(dwelling))
    area = obj.get("areaDwelling")
    _add("Area", f"{area} m²" if area else "")
    _add("Floor", _floor(_name_of(obj.get("floor"))))
    rooms = (obj.get("sleepingRoom") or {}).get("amountOfRooms")
    _add("Bedrooms", rooms if rooms not in (None, "", "0") else "")
    _add("Address", f"{address}, {city}" if address else "")
    _add("Postcode", (obj.get("postalcode") or "").strip())
    _add("Neighborhood", _name_of(obj.get("neighborhood")))

    # 租客维度。**逐条读站点自己的 doelgroepen**，不是整站断言——同一批里
    # student 与 regulier 并存，一刀切会把非学生盘也标成学生盘。
    codes = {d.get("code") for d in (obj.get("doelgroepen") or [])
             if isinstance(d, dict)}
    if codes == {_STUDENT_CODE}:
        _add("Tenant", "student only")
    _add("Target group", ", ".join(sorted(c for c in codes if c)) or "")

    net = obj.get("netRent")
    if net not in (None, "", 0) and float(net) != rent:
        _add("Net rent", _fmt_euro(float(net)))
    _add("Construction year", obj.get("constructionYear") or "")
    closing = (obj.get("closingDate") or "").strip()
    if closing and not closing.startswith("0000"):
        _add("Closing date", closing)
    # 应征需要付费注册（€27,50/年）。收到通知却没注册就动不了，必须说出来。
    if obj.get("inschrijvingVereistVoorReageren"):
        _add("Registration", "required to respond (paid account)")

    url_key = (obj.get("urlKey") or "").strip()
    available = (obj.get("availableFromDate") or "").strip()

    return Listing(
        id=f"pz_{oid}",
        name=f"{address}, {city}".strip().strip(",") or f"Plaza {oid}",
        status="Available to book",
        price_raw=_fmt_euro(rent),
        available_from=available or None,
        features=features,
        url=f"{BASE_URL}{DETAIL_PATH}{url_key}" if url_key else BASE_URL,
        city=city,
        source="plaza",
    )


class PlazaScraper(AbstractScraper):
    """单元级 scraper（一个 POST 拿全站，纯 HTTP，无反爬）。"""

    source = "plaza"

    def __init__(self) -> None:
        #: 本批次的解析结果。``batch_session()`` 进入时抓一次，各城市 task 复用。
        self._listings: Optional[list[Listing]] = None
        self._complete: bool = True

    # ── 取数 ────────────────────────────────────────────────────────

    def _fetch(self) -> dict:
        """打一次 ``getallobjects``。

        走抓取代理链与其余 source 一致；代理全部冷却时 ``get_proxy_url`` 返回空串，
        此处降级直连。**代理为空时必须显式传 ``NO_PROXY_CURL``，不能传 {}**：curl
        拿到空字典会回落到 ``HTTP_PROXY`` / ``HTTPS_PROXY`` 环境变量，也就是回到那个
        刚被判定为失效的代理。ourdomain.py:280 与 magis.py 各有一份实测记录。

        传输层异常一律包成 ``ScrapeNetworkError``：dispatcher 只在
        ``except ScrapeNetworkError`` 那一支里调 ``is_proxy_error``，裸的
        ``curl_cffi.ProxyError`` 会被记成「未预期异常」，本 source 就永远不参与代理
        冷却判定（2026-09-02 在 magis 与 studentexperience 上实测过这个洞）。
        """
        import curl_cffi.requests as req

        from config import get_impersonate, get_proxy_url
        from net import NO_PROXY_CURL

        proxy = get_proxy_url(self.source)
        proxies = {"http": proxy, "https": proxy} if proxy else NO_PROXY_CURL
        with req.Session(impersonate=get_impersonate(), proxies=proxies) as session:
            try:
                resp = session.post(
                    LIST_URL, timeout=40, json={},
                    headers={
                        "Accept": "application/json",
                        "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
                        "X-Requested-With": "XMLHttpRequest",
                    },
                )
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as e:
                raise ScrapeNetworkError(
                    f"Plaza 房源接口请求失败: {type(e).__name__}: {e}") from e
            if resp.status_code != 200:
                raise ScrapeNetworkError(
                    f"Plaza 房源接口返回 HTTP {resp.status_code}")
            try:
                return resp.json()
            except Exception as e:
                # 站点在路由不认识时会返回一整页 HTML 而不是 JSON（实测 /api/v1/*
                # 的 404 就是 158 KB 的 HTML）。那是「接口没了」，不是「没房源」。
                raise ScrapeNetworkError(
                    f"Plaza 房源接口返回的不是 JSON（{len(resp.content)} 字节）"
                ) from e

    @contextmanager
    def batch_session(self):
        """整批共用一次 HTTP。

        一个 POST 就返回全部城市的全部房源；按城市各发一次不但没必要，还会把对同一
        个端点的请求频率乘上城市数，而且两次请求之间库存可能变化——先到先得那 31 条
        广告在首个回应后即关闭，两份快照拼出来的结果会自相矛盾。
        """
        self._listings = None
        self._complete = True
        try:
            yield
        finally:
            self._listings = None
            self._complete = True

    def invalidate_session(self) -> None:
        self._listings = None
        self._complete = True

    # ── 抓取 ────────────────────────────────────────────────────────

    def _parse_all(self, payload: dict) -> tuple[list[Listing], bool]:
        """整份响应 → (listings, 是否完整)。"""
        # ``sAngularServiceData`` 里带着门户配置，无论有没有房源都返回。拿它当结构
        # 探针：读得到就说明这是一份真的接口响应，此时 ``result`` 为空可以放心当成
        # 「当前没有在架房源」；读不到则说明拿到的是别的东西（改版、网关、登录墙），
        # 不能把空结果当成「没房源」——那会让存量被整体收敛成 Occupied 并发一批假
        # 的下架通知。
        probe_ok = False
        try:
            inner = json.loads(payload.get("sAngularServiceData") or "[]")
            probe_ok = any(
                "getportalconfiguration" in (e.get("url") or "")
                for e in inner if isinstance(e, dict))
        except Exception:
            probe_ok = False

        rows = payload.get("result")
        if not isinstance(rows, list) or not probe_ok:
            logger.warning(
                "Plaza 响应不是预期结构（result=%s，配置探针=%s），本轮标记不完整",
                type(rows).__name__, probe_ok)
            return [], False

        listings: list[Listing] = []
        seen: set[str] = set()
        skipped_foreign = skipped_nonhousing = dropped = 0
        for obj in rows:
            if not isinstance(obj, dict):
                dropped += 1
                continue
            if (obj.get("dwellingType") or {}).get("categorie") != HOUSING_CATEGORY:
                skipped_nonhousing += 1
                continue
            if str((obj.get("land") or {}).get("id") or "") != NL_LAND_ID:
                skipped_foreign += 1
                continue
            item = _parse_object(obj)
            if item is None:
                dropped += 1
                continue
            if item.id in seen:
                continue
            seen.add(item.id)
            listings.append(item)

        housing_nl = len(listings) + dropped
        # 荷兰住宅里过半认不出 → 上游字段改了。此时「抓到几条」比「一条没抓到」更
        # 危险：它看起来像正常结果。与 magis 同一判据。
        complete = dropped * 2 <= housing_nl if housing_nl else True
        if dropped:
            logger.warning("Plaza 有 %d/%d 条荷兰住宅缺关键字段，已跳过",
                           dropped, housing_nl)
        logger.info(
            "Plaza 共解析 %d 条荷兰住宅（响应 %d 条：非住宅 %d、非荷兰 %d、跳过 %d）",
            len(listings), len(rows), skipped_nonhousing, skipped_foreign, dropped)
        return listings, complete

    def scrape(self, task: ScrapeTask) -> ScrapeResult:
        if self._listings is None:
            self._listings, self._complete = self._parse_all(self._fetch())

        if not self._complete:
            return ScrapeResult(task=task, listings=[], complete=False,
                                error="unexpected response shape")

        mine = [x for x in self._listings if x.city == task.city_display]

        # 未登记城市的房源不会被静默丢掉——清单是 2026-09-02 的快照，站点上架新城市
        # 时这里就是唯一能发现的地方。见模块文档「城市清单会漂」。
        unknown = sorted({x.city for x in self._listings if x.city not in CITIES})
        if unknown:
            logger.warning(
                "Plaza 出现未登记的城市 %s——这些房源不会分派给任何 task，"
                "把它们加进 config.KNOWN_PLAZA_CITIES 即可", unknown)

        logger.info("[%s] Plaza 共 %d 条房源（全站 %d 条）",
                    task.city_display, len(mine), len(self._listings))
        return ScrapeResult(task=task, listings=mine, complete=True)
