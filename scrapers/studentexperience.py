"""
scrapers/studentexperience.py — Student Experience scraper
===========================================================

Student Experience（studentexperience.com）自营学生公寓，荷兰五处在营：
Amsterdam Minervahaven / Zuidas / NDSM / Amstel，以及 Leiden
（另有 Amstelveen Uilenstede 标注 "Under development"，不在监控范围）。
西班牙另有 Granada 与 Madrid Pozuelo，本 scraper 只取荷兰。

传输
----
纯 HTTP，服务端渲染。**没有 Cloudflare、没有 JS 挑战、不需要浏览器**——普通
``Mozilla/5.0`` 直接 200（2026-09-01 实测）。与 Magis 同级，是已接平台里最省事
的两个之一。

两条预订线，同一套卡片标记
--------------------------
站点把库存切成两条互不相交的线，各有各的入口：

    短租  /studios?los=shortstay&locationId=<id>&academicTermId=<term>
    长租  /studios?los=longstay

短租按**学期档**售卖，档期由 ``/locations/getAcademicTerms/<locationId>`` 给出
（JSON）；没有档期的楼盘在短租线上什么都不返回。长租是「最少一年合同」，不分档，
一个 URL 出全部荷兰楼盘。

两条线渲染同一套卡片 DOM（``<a class="studio is-overview …">``），所以解析只需
一份。差别只在**怎么把 URL 拼出来**。

⚠️ ``locationId`` 在长租路径上**被忽略**：传 ``locationId=8``（Granada）返回的
仍是荷兰四栋楼的计数（2026-09-01 实测）。所以长租每轮只发一次请求，不按楼盘循环。

「有卡片」即「有货」
--------------------
站点只渲染当前可订的户型，售罄的直接不出现——2026-09-01 实测：Minervahaven 有货
时渲染 2 张卡片，Zuidas / NDSM / Amstel / Leiden 各 0 张。

因此本 scraper 产出的 Listing 状态恒为 ``Available to book``；「消失」的语义由
monitor 的 stale 收敛处理（本 source 不在 ``sources_with_full_lifecycle`` 里，
消失走 Reserved → Occupied 两跳，与 magis / xior / ourdomain 一致）。

「Or explore our other studios」滑块里的紧凑卡片同样只列有货的户型，不是全量目录
——上面那次实测里 0 库存的楼盘连滑块都不渲染。所以两种卡片一视同仁地收。

完整性探针：长租页的计数块
--------------------------
这是本 scraper 唯一不显然的地方，值得写清楚。

短租路径上「0 张卡片」有三种成因，从 HTML 上**分不出来**：真的没货、没选学期档、
或者站点改版把卡片类名换了。三者里只有第三种该判 incomplete，可是页面在前两种
情况下也一样干干净净——连 "we don't have available studios" 那句提示都不出
（2026-09-01 实测 ``?los=shortstay&locationId=3``）。

长租页不同：它**总是**渲染一个按楼盘的计数块

    Complex   Amsterdam Amstel 0   Amsterdam NDSM 0   Amsterdam Zuidas 0   Leiden 0

有货没货都在。所以拿它当结构探针——计数块认不出来，就是站点改版了，整轮判
incomplete；认得出来则说明 DOM 还是我们认识的那个，此时「0 张卡片」可以放心地
当成「真的没货」。

这条探针还顺带给出一个交叉校验：计数之和 > 0 却一张长租卡片都没解析出来，同样
判 incomplete——那是卡片类名变了而计数块没变的情形。

价格：报的是 "From"
-------------------
卡片上的价格是该户型的**起价**（``<span class="studio-price-label">From</span>``），
不是某一间的确切租金——一个户型对应多间，面积本身就是区间（"20,5-26 m²"）。
含 VAT、水电、市政税、Wi-Fi 与双周保洁，口径上已经是到手价，不需要像 OurDomain
那样加注（见 ``models.rent_basis_note``）。

押金另计，写进 features 的 ``Deposit`` 一条，不参与筛选。

面积区间与 min_area
-------------------
面积写成 ``Area: 20,5-26 m²`` 原样。这不是偷懒：``models.parse_float`` 对区间取
**下界**（实测 ``"20,5-26 m²" → 20.5``），而 ``min_area`` 是 fail-closed 的
「至少多大」，取下界正是这里该有的保守语义——用户要 ≥25 m² 时，不该因为这个户型
里恰好有几间 26 m² 就把整个户型推给他。

通知里用户看见的是完整区间，筛选用的是下界，两边都对。

维度登记
--------
- ``tenant`` 登记为 ``student only``：FAQ 原文「All Student Experience studios
  are exclusively available for students」，签约前必须上传在读证明。这是整个
  source 的事实、不按户型变，所以走 ``SOURCE_ASSUMED_FEATURES``，与 xior 同一
  处理。合格身份含 study programme / internship / **PhD research**——与
  OurCampus 相反（那边 PhD 与博后明确不合格）。
- ``type`` 登记：站点全部产品都是 studio，四个档位名（Signature / Essential /
  Prestige / Comfort）是价位分层不是房型。只在类型名里出现 "studio" 时才写
  ``Type: Studio``，认不出就不写。
- ``finishing`` **不登记**。规格行（"Private & fully furnished"）只出现在主卡片
  上，滑块里的紧凑卡片没有这一行。这个维度是 fail-closed 的，登记之后紧凑卡片
  那几条会被勾了装修档位的用户整体过滤掉——与 magis 的 tenant 同一个取舍：值仍
  写进 features 让通知里看得见，但不参与筛选。
- ``floor`` / ``energy`` 站点不给，不登记。楼层信息只在设施行的散文里出现过
  （"located on floor 5-8"），是户型的整体描述而非某一间的楼层，解析出来也没有
  对应语义。

短租的额外资格条件
------------------
Minervahaven 短租线要求：临时居留（≤1 年）、荷兰境外常住地址、外国国籍，**不接受
用荷兰地址提交的申请**。这几条没有对应的筛选维度，也不适合塞进 tenant（那个维度
的取值表是「学生/青年职场」这一类身份，不是居留状态），因此只在本注释里记录，
不写进 Listing。用户看到房源后仍需自行核对 FAQ。

不做自动预订
------------
FAQ 明确「first-come, first-served」——这正是接入本平台的理由，也意味着下单窗口
很短。但下单流程未做侦察，也未评估 ToS 暴露面。与 OurCampus / Magis 一致：
只通知，不预订。

规模与节奏
----------
2026-09-01 侦察：5 栋楼，当时全站可订 2 个户型（均在 Minervahaven）。库存是
间歇性的——站点自己提供「有房时邮件通知我」的订阅，用户实测同一天内看到过房源
出现又消失。轮询节奏是否需要比其余 source 更密，见 ``docs/SCRAPING_RECON.md``。
"""
from __future__ import annotations

import html as html_mod
import json
import logging
import re
from contextlib import contextmanager
from typing import Optional

from models import Listing, parse_float

from .base import AbstractScraper, ScrapeNetworkError, ScrapeResult, ScrapeTask

logger = logging.getLogger(__name__)


BASE_URL = "https://studentexperience.com"
STUDIOS_URL = f"{BASE_URL}/studios"
TERMS_URL = f"{BASE_URL}/locations/getAcademicTerms"


#: 荷兰在营楼盘：locationId → (楼盘名, 城市)。
#:
#: id 取自 ``/studios`` 的 ``<select name="locationId">``。Amstelveen Uilenstede
#: 标注 "Under development"，站点未给它 locationId，因此不在表内；西班牙的
#: Granada(8) / Madrid Pozuelo(7) 有 id 但不属于本项目范围。
LOCATIONS: dict[int, tuple[str, str]] = {
    2:  ("Amsterdam Minervahaven", "Amsterdam"),
    3:  ("Amsterdam Zuidas",       "Amsterdam"),
    4:  ("Amsterdam NDSM",         "Amsterdam"),
    5:  ("Amsterdam Amstel",       "Amsterdam"),
    36: ("Leiden",                 "Leiden"),
}

#: 本平台覆盖的城市（去重后）。
CITIES = ("Amsterdam", "Leiden")


# ── 卡片解析 ────────────────────────────────────────────────────────

#: 一张卡片。两种变体（主卡片 has-popularity-header / 滑块里的 studio-compact）
#: 共用这个 class 前缀，一条正则都收。
_CARD = re.compile(
    r'<a\s+href="([^"]*?/studio-types/(\d+)[^"]*)"\s+class="studio is-overview([^"]*)"',
    re.I,
)

_TAG = re.compile(r"<[^>]+>")

_RE_FACIL_HEAD = re.compile(
    r'<p class="studio-facilities-heading">(.*?)</p>', re.S)


def _blocks(segment: str, tag: str, cls: str) -> list[str]:
    """取出 ``<tag class="cls…">…</tag>`` 的**内层标记**，正确处理同名嵌套。

    为什么不能用 ``<span class="x">(.*?)</span>`` 这种非贪婪正则：站点的两个字段
    内部各嵌了一个同名标签——

        <span class="studio-price-deposit">
            €1.750 deposit
            <span class="studio-price-deposit-separator">·</span>
            <strong>fully refundable</strong>
        </span>

    非贪婪会停在内层的 ``</span>``，押金被截成「€1.750 deposit ·」，装修档位
    （挤在面积后面、由 ``studio-spec-separator`` 隔开）则整个丢失。这两处不是
    边角情况：它们分别是押金和装修档位，都要出现在通知里。

    所以按开合标签计数找配对结尾。``class`` 用前缀匹配（站点的 class 常带修饰，
    如 ``studio is-overview shortstay``），因此 ``cls`` 只需给出开头那一段。
    """
    out: list[str] = []
    opener = re.compile(rf'<{tag}\b[^>]*class="{re.escape(cls)}[^"]*"[^>]*>', re.I)
    any_tag = re.compile(rf"<(/?){tag}\b[^>]*>", re.I)
    for m in opener.finditer(segment):
        depth, pos = 1, m.end()
        while depth and (t := any_tag.search(segment, pos)):
            depth += -1 if t.group(1) else 1
            pos = t.end()
        if depth == 0:
            out.append(segment[m.end():pos - len(t.group(0))])
    return out


def _first(segment: str, tag: str, cls: str) -> str:
    """``_blocks`` 的头一个，取不到返回空串。"""
    hits = _blocks(segment, tag, cls)
    return hits[0] if hits else ""

_RE_AREA = re.compile(r"^([\d.,]+(?:\s*-\s*[\d.,]+)?)\s*m²$")
#: 地址行以邮编 + 城市收尾，用它把地址行从其余 meta 行里认出来。
_RE_ADDRESS = re.compile(r"\d{4}\s*[A-Z]{2}\s+\S")
_RE_ONLY_N = re.compile(r"Only\s+(\d+)\s+studios?\s+available", re.I)
#: 通勤时间行，"12 mins to nearest shops" 一类。
_RE_TRAVEL = re.compile(r"^\d+\s*mins?\s+to\b", re.I)

#: 长租页的按楼盘计数块。**这是完整性探针**，见模块文档。
_RE_COMPLEX_BLOCK = re.compile(r"Complex(.*?)(?:It seems|Sort by|$)", re.S)
_RE_COMPLEX_ITEM = re.compile(r"([A-Z][\w\s]{2,28}?)\s+(\d+)\b")


def _text(fragment: str) -> str:
    """标记片段 → 归一化的可见文本。"""
    return re.sub(r"\s+", " ", html_mod.unescape(_TAG.sub(" ", fragment))).strip()


def _plain(page: str) -> str:
    """整页 → 可见文本（剥掉 script/style，它们含大量 data-label 模板串）。

    不剥的话 header 里的 ``data-label-want-studio="I want this studio"`` 会被
    当成一张卡片的按钮文本——2026-09-01 就是这么数错过一次可订数量。
    """
    body = re.sub(r"(?is)<(script|style|svg|head)[^>]*>.*?</\1>", " ", page)
    return re.sub(r"\s+", " ", html_mod.unescape(_TAG.sub(" ", body))).strip()


def _split_cards(page: str) -> list[tuple[str, str, str]]:
    """整页 → [(url, type_id, segment)]。

    按 ``<a class="studio is-overview…">`` 切；每张卡片切到下一张卡片的开头，
    最后一张切到页尾。卡片内部不会再出现同样的锚点，所以这么切是安全的。
    """
    hits = list(_CARD.finditer(page))
    cards: list[tuple[str, str, str]] = []
    for i, m in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(page)
        url = html_mod.unescape(m.group(1))
        if url.startswith("/"):
            url = BASE_URL + url
        cards.append((url, m.group(2), page[m.start():end]))
    return cards


def _parse_card(url: str, type_id: str, segment: str,
                term: str = "") -> Optional[Listing]:
    """一张卡片 → Listing；认不出楼盘或价格时返回 None。

    「关键」只有楼盘和价格两项。楼盘决定这条属于哪个城市（没有它就无法分派给
    ScrapeTask），价格缺失会让房源被所有带租金上限的筛选整体漏掉——与其余
    scraper 同一判据。
    """
    residence = _text(_first(segment, "span", "studio-title-location"))
    if not residence:
        return None

    price = parse_float(_text(_first(segment, "span", "studio-price-amount")))
    if price is None:
        return None

    city = next((c for loc, c in LOCATIONS.values() if loc == residence), "")
    if not city:
        # 楼盘名不在表里：站点新开了一处，或者改了名字。不猜城市——猜错会把房源
        # 分派给错误的 ScrapeTask，用户按城市订阅就会收到不该收的。
        logger.warning("Student Experience 出现未登记的楼盘 %r（studio-type %s）",
                       residence, type_id)
        return None

    studio_type = _text(_first(segment, "span", "studio-title-type"))

    features: list[str] = []

    def _add(key: str, value: object) -> None:
        if value not in (None, ""):
            features.append(f"{key}: {value}")

    # meta 行的顺序不保证，各认各的模式（与 magis 同一策略）。
    #
    # 通勤时间那几行（"12 mins to nearest shops"）的 class 是
    # ``studio-meta-item studio-meta-item-travel-time``，前缀匹配也会收进来，
    # 按文本模式剔除——比收紧 class 匹配稳，站点加个修饰类不会让整块失效。
    area = furnishing = address = ""
    for raw in _blocks(segment, "div", "studio-meta-item"):
        # 规格行是「面积 + 分隔符 + 装修」两段挤在同一个 span 里，先按分隔标签切开
        for part in re.split(r'<span class="studio-spec-separator"[^>]*>\s*</span>',
                             raw):
            piece = _text(part)
            if not piece or _RE_TRAVEL.match(piece):
                continue
            mm = _RE_AREA.match(piece)
            if mm and not area:
                area = mm.group(1).replace(" ", "")
            elif _RE_ADDRESS.search(piece) and not address:
                address = piece
            elif "furnish" in piece.lower() and not furnishing:
                furnishing = piece

    _add("Building", residence)
    if "studio" in studio_type.lower():
        _add("Type", "Studio")
    _add("Studio type", studio_type)
    # 区间原样写。parse_float 取下界，正是 min_area 该有的保守语义（见模块文档）。
    _add("Area", f"{area} m²" if area else "")
    # 装修档位写进 features 但**不登记为筛选维度**——紧凑卡片没有这一行，
    # 登记会让那几条被 fail-closed 整体拒掉（见模块文档「维度登记」）。
    _add("Finishing note", furnishing)
    _add("Address", address)
    _add("Length of stay", term)

    # 稀缺徽标只在余量少时出现，**不能当成余量字段**——没有徽标不代表没货，
    # 只代表站点不觉得需要催。
    mm = _RE_ONLY_N.search(_text(_first(segment, "span", "badge")))
    _add("Units left", mm.group(1) if mm else None)

    _add("Deposit", _text(_first(segment, "span", "studio-price-deposit")))

    m = _RE_FACIL_HEAD.search(segment)
    if m:
        _add("Included", _text(m.group(1)).rstrip(":"))

    return Listing(
        id=f"se_{type_id}",
        name=f"{residence} — {studio_type}".strip().strip("—").strip(),
        status="Available to book",
        price_raw=f"€{price:,.0f}".replace(",", "."),
        available_from=None,
        features=features,
        url=url,
        city=city,
        source="studentexperience",
    )


def _parse_complex_counts(page: str) -> dict[str, int]:
    """长租页的按楼盘计数块 → {楼盘名: 可订数}。认不出返回空 dict。

    调用方拿空 dict 当「站点改版」的信号——这个块在有货没货时都渲染，
    读不到就说明 DOM 不是我们认识的那个了。
    """
    m = _RE_COMPLEX_BLOCK.search(_plain(page))
    if not m:
        return {}
    known = {name for name, _ in LOCATIONS.values()}
    return {name.strip(): int(n)
            for name, n in _RE_COMPLEX_ITEM.findall(m.group(1))
            if name.strip() in known}


# ── Scraper ────────────────────────────────────────────────────────

class StudentExperienceScraper(AbstractScraper):
    """户型级 scraper（纯 HTTP，无反爬）。"""

    source = "studentexperience"

    def __init__(self) -> None:
        #: 本批次的解析结果。``batch_session()`` 进入时抓一次，各城市 task 复用。
        self._listings: Optional[list[Listing]] = None
        self._complete: bool = True

    # ── 取页 ────────────────────────────────────────────────────────

    def _session(self):
        """建一条与其余 source 同策略的 HTTP 会话。

        站点本身没有反爬，走代理只是为了共用同一条出口策略——代理全部冷却时
        ``get_proxy_url`` 返回空串，此处降级直连。

        代理全部冷却时必须显式传 ``NO_PROXY_CURL``，**不能传 {}**：curl 拿到空
        字典会回落到 HTTP_PROXY / HTTPS_PROXY 环境变量，也就是回到那个刚被判定
        为失效的代理，于是「降级直连」从来没有真的直连过。同一段坑
        ourdomain.py:280 与 magis.py 各有一份实测记录。
        """
        import curl_cffi.requests as req

        from config import get_impersonate, get_proxy_url
        from net import NO_PROXY_CURL

        proxy = get_proxy_url(self.source)
        proxies = {"http": proxy, "https": proxy} if proxy else NO_PROXY_CURL
        return req.Session(impersonate=get_impersonate(), proxies=proxies)

    @staticmethod
    def _get(session, url: str, params: Optional[dict] = None) -> str:
        resp = session.get(url, params=params, timeout=30,
                           headers={"Accept-Language": "en-US,en;q=0.9"})
        if resp.status_code != 200:
            raise ScrapeNetworkError(
                f"Student Experience {url} 返回 HTTP {resp.status_code}")
        return resp.text

    def _fetch_all(self) -> tuple[list[Listing], bool]:
        """抓一轮，返回 (全部城市的 listings, 本轮是否完整)。

        请求预算：长租 1 次 + 学期档 5 次 + 有档期的楼盘各 1 次 ≈ 7 次。
        全部在 ``batch_session()`` 里做一次，两个城市的 task 共用。
        """
        listings: list[Listing] = []
        seen: set[str] = set()

        def _collect(page: str, term: str = "") -> int:
            added = 0
            for url, type_id, segment in _split_cards(page):
                item = _parse_card(url, type_id, segment, term)
                if item is None or item.id in seen:
                    continue
                seen.add(item.id)
                listings.append(item)
                added += 1
            return added

        with self._session() as session:
            # ── 长租：一次出全部荷兰楼盘，并兼作结构探针 ──
            long_page = self._get(session, STUDIOS_URL, {"los": "longstay"})
            counts = _parse_complex_counts(long_page)
            if not counts:
                # 计数块在有货没货时都渲染，读不到只可能是站点改版。
                logger.warning(
                    "Student Experience 长租页读不到楼盘计数块（%d 字节），"
                    "本轮标记不完整", len(long_page))
                return [], False

            long_cards = _collect(long_page)
            expected = sum(counts.values())
            if expected > 0 and long_cards == 0:
                # 计数说有货、卡片一张都没解析出来 → 卡片类名变了。
                logger.warning(
                    "Student Experience 长租计数为 %d 但解析出 0 张卡片，"
                    "本轮标记不完整（计数：%s）", expected, counts)
                return [], False

            # ── 短租：按楼盘取学期档，有档期的才去取卡片 ──
            for loc_id, (residence, _city) in LOCATIONS.items():
                try:
                    raw = self._get(session, f"{TERMS_URL}/{loc_id}")
                    terms = (json.loads(raw) or {}).get("terms") or []
                except ScrapeNetworkError:
                    raise
                except Exception:
                    # 单个楼盘的档期读不出来不该毁掉整轮：其余楼盘的结果仍然有效，
                    # 只是这一处这轮看不到短租房源。标记不完整让 monitor 跳过收敛。
                    logger.warning("Student Experience %s 的学期档解析失败",
                                   residence, exc_info=True)
                    self._complete = False
                    continue

                for t in terms:
                    term_id = str(t.get("yardiAcademicTermIdValue") or "").strip()
                    term_name = str(t.get("academicTermName") or "").strip()
                    if not term_id:
                        continue
                    page = self._get(session, STUDIOS_URL, {
                        "los": "shortstay",
                        "locationId": loc_id,
                        "academicTermId": term_id,
                    })
                    _collect(page, term_name)

        logger.info("Student Experience 共抓取 %d 条户型（长租计数：%s）",
                    len(listings), counts)
        return listings, self._complete

    @contextmanager
    def batch_session(self):
        """整批共用一轮 HTTP。

        荷兰只有 Amsterdam 与 Leiden 两个城市，但长租那一页一次就返回全部楼盘；
        按城市各抓一轮会把对同一批 URL 的请求频率翻倍，且两轮之间库存可能变化，
        反而给出自相矛盾的快照。
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

    def scrape(self, task: ScrapeTask) -> ScrapeResult:
        if self._listings is None:
            self._listings, self._complete = self._fetch_all()

        if not self._complete:
            return ScrapeResult(task=task, listings=[], complete=False,
                                error="page structure not recognised")

        mine = [x for x in self._listings if x.city == task.city_display]
        logger.info("[%s] Student Experience 共 %d 条户型（全站 %d 条）",
                    task.city_display, len(mine), len(self._listings))
        return ScrapeResult(task=task, listings=mine, complete=True)
