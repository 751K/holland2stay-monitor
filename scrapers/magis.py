"""
scrapers/magis.py — Magis Real Estate scraper
==============================================

Magis（magisrealestate.com）自建 Laravel + Livewire 站点，在 Tilburg、Eindhoven、
's-Hertogenbosch、Amersfoort、Rijswijk 五个城市共 17 栋楼出租。

传输
----
纯 HTTP，服务端渲染。**没有 Cloudflare、没有 JS 挑战、不需要浏览器、不需要代理**
——普通 ``Mozilla/5.0`` 直接 200（2026-09-01 实测 141 KB / 3.2s）。``robots.txt``
是空 ``Disallow:``，全站允许抓取。这是四个已接平台里最省事的一个。

一次请求拿全站
--------------
其余三个平台都要按城市/楼栋分别请求，Magis 的 ``/for-rent`` 一次返回全部城市的
全部单元。因此**每轮只发一次 HTTP**：在 ``batch_session()`` 里抓一次并缓存，
各城市的 ScrapeTask 从同一份 HTML 里按城市筛。

``?only_available=0`` 是关键：Livewire 的 ``only_available`` 属性同步到 query
string，置 0 之后连 ``Not available`` 的单元一起返回（2026-09-01 实测 4 → 12 条）。
本项目要的正是这个——状态变更通知的原料是「同一个单元从可租变成不可租」，只抓
可租的就只能看见「消失」，那是有歧义的。

价格口径：报到手价
------------------
卡片同时给出基础租金和**服务费的确切金额**（"service costs, furniture,
utilities, internet & TV amount to € 121,51 per month"，12 条实测全都有）。因此
``price_raw`` 直接报两者之和，与 Holland2Stay / Xior 同一口径，而不是像
OurDomain / OurCampus 那样只能标注——它们拿不到单元的户型，服务费按户型变，无法
合成（见 models.rent_basis_note）。

拆分保留在 features 的 ``Base rent`` / ``Service costs amount`` 两条里，不参与
筛选，只是让用户核得动这个数。

解析：按模式而不是按位置
------------------------
卡片的可见文本顺序**不稳定**。同一批 12 条里，第 9 行在一张卡上是面积、在另一张
上是「External storage room」——设施行的有无会把后面全部顶掉一位。所以每个字段
各用各的模式去认（面积认 ``m²``、楼层认 ``Nth floor``、能耗认单独一行的
``A+``/``B``），一条认不出只丢那一条，不会让整张卡错位。

规模
----
2026-09-01 侦察：在架 12 套，其中 4 套 Available；组合里 5 城 17 栋，Eindhoven
占 9 栋（Aalsterweg×2 / Boschdijk / Driek / The General / Kloosterdreef /
Montgomerylaan / Woenselse Markt / Zernikestraat），正好是本项目的主力城市。

量级远小于 H2S，接入赌的是这 17 栋楼的换手率。评估见 ``docs/SCRAPING_RECON.md``。

不做自动预订
------------
站点有 Account / Login，但下单流程未做侦察，也未评估 ToS 暴露面。与 OurCampus
一致：只通知，不预订。
"""
from __future__ import annotations

import html as html_mod
import logging
import re
from contextlib import contextmanager
from urllib.parse import unquote
from typing import Optional

from models import Listing, parse_float

from config import _FIN_FULLY, _FIN_FURNISHED, _FIN_SEMI, _FIN_UNFURNISHED

from .base import AbstractScraper, ScrapeNetworkError, ScrapeResult, ScrapeTask

logger = logging.getLogger(__name__)


#: 房源总览页。``only_available=0`` 见模块文档。
LIST_URL = "https://magisrealestate.com/for-rent"
LIST_PARAMS = {"only_available": "0"}

#: 详情页链接的形状：``/for-rent/{楼栋 slug}/{单元号}``。
#: 单元号里有空格（``2 F329`` → ``2%20F329``），所以不能用 ``[^\s"]``。
_CARD_HREF = re.compile(
    r'href="(https://magisrealestate\.com/for-rent/([^/"]+)/([^"#?]+))"'
)

_TAG = re.compile(r"<[^>]+>")

#: 各字段的识别模式。顺序不稳定（见模块文档），一律按内容认。
_RE_STATUS = re.compile(r"^(Available|Not available)$", re.I)
_RE_AREA = re.compile(r"^([\d.,]+)\s*m²$")
_RE_FLOOR = re.compile(r"^(?:(\d+)(?:st|nd|rd|th)\s+floor|(Ground)\s+floor)$", re.I)
_RE_ENERGY = re.compile(r"^(A\+{0,3}|[B-G])$")
_RE_PRICE = re.compile(r"^€\s*([\d.,]+)$")
_RE_SERVICE = re.compile(r"amount to\s*€\s*([\d.,]+)\s*per month", re.I)
_RE_STREET = re.compile(r"^,\s*(.+)$")

#: 站点自己的户型词表（Livewire 组件的 ``allTypes``）。
UNIT_TYPES = ("studio", "2-room apartment", "3-room apartment",
              "room in shared apartment")

#: 站点自己的城市词表（Livewire 组件的 ``locations``）。
CITIES = ("'s-Hertogenbosch", "Amersfoort", "Eindhoven", "Rijswijk", "Tilburg")

#: 站点措辞 → 本项目的装修档位词表（``app/i18n.DEFAULT_FINISHING`` 的四档）。
#:
#: 站点另有 "Padded (floor and curtains)" / "Not padded"，说的是地面与窗帘的铺装，
#: 不是家具。筛选这个维度是**整体相等**匹配（``_EXACT_MATCH_DIMS``），把铺装混进
#: 来会造出词表之外的取值，用户勾任何一档都匹配不上。
#: 取值用 config 的规范常量，不在这里新造词——过滤下拉是按库里出现过的值去重
#: 出来的，多一个写法就多一个选项。
FINISHING_MAP = {
    "fully furnished": _FIN_FULLY,
    "furnished": _FIN_FURNISHED,
    "semi furnished": _FIN_SEMI,
    "not furnished": _FIN_UNFURNISHED,
}

#: 站点的租客徽标 → 本项目的 tenant 词表。
#:
#: 12 条实测里只出现过 "Students only"（3 条）。站点的筛选另有 "Starters" 这一档，
#: 但没有任何一条房源带它的徽标，所以**它的措辞与语义都还没见过**，不猜。
#: 没有徽标的卡片一概不写 Tenant——「没徽标」到底是「不限」还是「未标注」同样
#: 没有证据，而这个维度是 fail-closed 的，写错会把人挡在外面。
#:
#: 也正因如此 magis 暂不登记 tenant 维度（见 config._SOURCE_FILTER_DIMS）：
#: 登记之后没有徽标的 9/12 条会被勾了租客条件的用户整体过滤掉。
TENANT_MAP = {
    "Students only": "student only",
}


def _euro(text: str) -> Optional[float]:
    """``"1.090,76"`` → ``1090.76``。

    直接用 ``models.parse_float``：它已经同时容忍英式与欧式千分位，站点这几种
    写法（``932,93`` / ``1.090,76`` / ``60,00``）逐个验过都对。这里留一层薄包装
    只是为了给站点的记法一个名字，别在别处又写一遍解析。
    """
    return parse_float(text)


def _fmt_euro(value: float) -> str:
    """``1054.44`` → ``"€1.054"``。

    取整到欧元：分位在通知里没有意义，而站点自己的服务费是月付估算值。
    千分位用点，和站点、和 Xior 的写法一致。
    """
    return "€" + f"{int(round(value)):,}".replace(",", ".")


def _card_lines(segment: str) -> list[str]:
    """一张卡片的 HTML → 去标签后的可见文本行。"""
    text = html_mod.unescape(_TAG.sub("\n", segment))
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def _split_cards(page: str) -> list[tuple[str, str, str, str]]:
    """整页 HTML → ``[(url, building_slug, unit, 卡片 HTML), ...]``。

    切法：以详情页链接为锚，从它前面最近的 ``<a`` 起到下一条房源链接之前。同一张
    卡里详情链接可能出现多次（图片和标题各一个），按 ``(楼栋, 单元)`` 去重后只留
    第一次出现的位置。
    """
    anchors: list[tuple[int, str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for m in _CARD_HREF.finditer(page):
        url = m.group(1)
        building = m.group(2)
        # 单元号里有空格（"2 F329" → "2%20F329"）。主键和展示名都要真实值，
        # 把 URL 编码原样带进 id 会让它在通知、深链、日志里全是 %20。
        unit = unquote(html_mod.unescape(m.group(3)))
        key = (building, unit)
        if key in seen:
            continue
        seen.add(key)
        anchors.append((m.start(), url, building, unit))

    out: list[tuple[str, str, str, str]] = []
    for i, (pos, url, building, unit) in enumerate(anchors):
        start = page.rfind("<a", 0, pos)
        if start < 0:
            start = pos
        end = anchors[i + 1][0] if i + 1 < len(anchors) else len(page)
        end = page.rfind("<a", start, end)
        out.append((url, building, unit, page[start:max(end, start)]))
    return out


def _parse_card(url: str, building: str, unit: str, segment: str) -> Optional[Listing]:
    """一张卡片 → Listing；认不出关键字段时返回 None。

    「关键」只有状态和价格两项：没有状态就无法判断它是不是新上架，没有价格就会被
    所有带租金上限的筛选整体漏掉（与 H2S 的处理一致）。其余字段缺了就不写，让
    对应维度的筛选按各自的 fail-open/closed 规则处理。
    """
    lines = _card_lines(segment)
    if not lines:
        return None

    status = next((ln for ln in lines if _RE_STATUS.match(ln)), None)
    if not status:
        return None
    # 站点只有两种状态。归一到本项目的词表：Available 直接可租，
    # 其余一律 Occupied——Magis 没有「已预留待付款」这个中间态。
    status = "Available to book" if status.lower() == "available" else "Occupied"

    base = service = None
    for ln in lines:
        m = _RE_PRICE.match(ln)
        if m and base is None:
            base = _euro(m.group(1))
        m = _RE_SERVICE.search(ln)
        if m and service is None:
            service = _euro(m.group(1))
    if base is None:
        return None

    # 到手价。服务费认不出时只报基础租金，并在 features 里标注口径——
    # 静默按基础租金报会让这条房源在同一个租金上限下显得比实际便宜。
    all_in = base + (service or 0.0)

    features: list[str] = []

    def _add(key: str, value: object) -> None:
        if value not in (None, ""):
            features.append(f"{key}: {value}")

    city = next((ln for ln in lines if ln in CITIES), "")
    street = next((m.group(1) for ln in lines if (m := _RE_STREET.match(ln))), "")
    area = next((m.group(1) for ln in lines if (m := _RE_AREA.match(ln))), "")
    energy = next((m.group(1) for ln in lines if (m := _RE_ENERGY.match(ln))), "")
    # 户型：站点自己的词表。studio 出现在 "studio 8" 这种「户型 + 单元号」的行里，
    # 其余三种自成一行，startswith 两种都覆盖得到。
    utype = next((t for t in UNIT_TYPES
                  for ln in lines if ln.lower().startswith(t)), "")

    floor = ""
    for ln in lines:
        m = _RE_FLOOR.match(ln)
        if m:
            floor = "0" if m.group(2) else m.group(1)
            break

    furnishing = next((FINISHING_MAP[ln.lower()] for ln in lines
                       if ln.lower() in FINISHING_MAP), "")

    available_from = ""
    if "Available from" in lines:
        idx = lines.index("Available from")
        if idx + 1 < len(lines):
            available_from = lines[idx + 1]

    # 楼盘名是「•」前面那一行。不能取 lines[1]——带「Students only」徽标的卡片
    # 会在状态和楼盘名之间多一行，12 条实测里有 3 条是这样，取 lines[1] 会把
    # 租客标签当成楼盘名。
    building_name = building
    if "•" in lines:
        i = lines.index("•")
        if i > 0:
            building_name = lines[i - 1]

    # 租客限制。只登记看得见的徽标，**没有徽标不写**——见模块文档「租客维度」。
    tenant = TENANT_MAP.get(next((ln for ln in lines if ln in TENANT_MAP), ""), "")

    _add("Building", building_name)
    _add("Type", utype)
    _add("Area", f"{area} m²" if area else "")
    _add("Floor", floor)
    _add("Finishing", furnishing)
    _add("Tenant", tenant)
    _add("Energy", energy)
    # geocode 用整栋楼的街道（卡片只给街道名不给门牌，详情页才有完整门牌；
    # 街道 + 城市已经足够定位到楼，不值得为门牌每条多发一次请求）。
    _add("Address", f"{street}, {city}" if street and city else "")
    _add("Base rent", _fmt_euro(base))
    if service is not None:
        _add("Service costs amount", _fmt_euro(service))
    else:
        # 拿不到服务费时说明 price_raw 只是基础租金——models.rent_basis_note
        # 会把这条渲染到价格旁边。
        _add("Service costs", "excl., amount not published")

    return Listing(
        id=f"mg_{building}_{unit}".replace(" ", "-").replace("/", "-"),
        name=f"{building_name} {unit}, {city}".strip().strip(","),
        status=status,
        price_raw=_fmt_euro(all_in),
        available_from=_parse_date(available_from),
        features=features,
        url=url,
        city=city,
        source="magis",
    )


_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1)}

_RE_DATE = re.compile(r"^([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,\s*(\d{4})$")


def _parse_date(text: str) -> Optional[str]:
    """``"October 1st, 2026"`` → ``"2026-10-01"``；认不出返回 None。

    库里的 available_from 一律是 ISO：日历页与筛选都按字符串比较，混进一种
    英文写法会让它排到所有 ISO 日期之后，而不会报错。
    """
    m = _RE_DATE.match((text or "").strip())
    if not m:
        return None
    month = _MONTHS.get(m.group(1).lower())
    if not month:
        return None
    return f"{m.group(3)}-{month:02d}-{int(m.group(2)):02d}"


class MagisScraper(AbstractScraper):
    """Unit-level scraper for Magis Real Estate（纯 HTTP，无反爬）。"""

    source = "magis"

    def __init__(self) -> None:
        #: 本批次的页面缓存。``batch_session()`` 进入时抓一次，各城市 task 复用。
        self._page: Optional[str] = None

    # ── 取页 ────────────────────────────────────────────────────────

    def _fetch(self) -> str:
        """抓一次总览页。走抓取代理链，与其余 source 一致。

        站点本身没有反爬，走代理只是为了和其余 source 共用同一条出口策略——
        代理全部冷却时 ``get_proxy_url`` 返回空串，此处自然降级为直连。
        """
        import curl_cffi.requests as req

        from config import get_impersonate, get_proxy_url

        proxy = get_proxy_url(self.source)
        proxies = {"http": proxy, "https": proxy} if proxy else {}
        with req.Session(impersonate=get_impersonate(), proxies=proxies) as session:
            resp = session.get(LIST_URL, params=LIST_PARAMS, timeout=30,
                               headers={"Accept-Language": "en-US,en;q=0.9"})
            if resp.status_code != 200:
                raise ScrapeNetworkError(
                    f"Magis 总览页返回 HTTP {resp.status_code}"
                )
            return resp.text

    @contextmanager
    def batch_session(self):
        """整批共用一次 HTTP。

        Magis 的 ``/for-rent`` 一次给全部城市，按城市各发一次请求既没有必要，也会
        把对同一个页面的请求频率乘上城市数。
        """
        self._page = None
        try:
            yield
        finally:
            self._page = None

    def invalidate_session(self) -> None:
        self._page = None

    # ── 抓取 ────────────────────────────────────────────────────────

    def scrape(self, task: ScrapeTask) -> ScrapeResult:
        if self._page is None:
            self._page = self._fetch()
        page = self._page

        cards = _split_cards(page)
        if not cards:
            # 一条都切不出来：要么站点改版，要么拿到的是错误页。**不能当成
            # 「这一轮没有房」**——那会让存量房源被整体收敛成 Occupied 并发一批
            # 假通知。标记 incomplete，由 monitor 跳过收敛。
            logger.warning("Magis 总览页解析出 0 张卡片（%d 字节），本轮标记不完整",
                           len(page))
            return ScrapeResult(task=task, listings=[], complete=False,
                                error="no cards parsed")

        listings: list[Listing] = []
        dropped = 0
        for url, building, unit, segment in cards:
            item = _parse_card(url, building, unit, segment)
            if item is None:
                dropped += 1
                continue
            if item.city and item.city != task.city_display:
                continue
            listings.append(item)

        if dropped:
            logger.warning("Magis 有 %d/%d 张卡片缺状态或价格，已跳过",
                           dropped, len(cards))

        # 解析失败率过半时不认这一轮：结构变了的典型表现就是大部分卡片认不出，
        # 而此时「抓到几条」比「一条没抓到」更危险——它看起来像正常结果。
        complete = dropped * 2 <= len(cards)
        logger.info("[%s] Magis 共抓取 %d 条房源（全站 %d 张卡片，跳过 %d）",
                    task.city_display, len(listings), len(cards), dropped)
        return ScrapeResult(task=task, listings=listings, complete=complete)
