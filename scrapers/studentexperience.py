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
import time
from datetime import datetime
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

#: 一张卡片的锚点。
#:
#: 2026-09-04 站点改版，路径从 ``/studio-types/<id>`` 换成了 ``/studios/<id>``。
#: 旧正则要求前者，于是一张卡片都匹配不上——而页面明明有 12 张。
#:
#: 顺带把「href 必须紧挨 class」这个假设也去掉：先整体框住 ``<a …>``，再分别
#: 取两个属性。属性顺序不是契约，站点调一下顺序不该让整块失效。
_CARD_TAG = re.compile(r'<a\b[^>]*class="[^"]*\bstudio is-overview\b[^"]*"[^>]*>', re.I)
_CARD_HREF = re.compile(r'href="([^"]*/studios/(\d+)[^"]*)"', re.I)

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

#: 长租页的按楼盘计数。**这是完整性探针**，见模块文档。
#:
#: 认的是筛选表单里的复选框，不是可见文本：
#:
#:     <input type="checkbox" value="Amsterdam NDSM" name="complexes[]" …>
#:     <label …>Amsterdam NDSM</label>
#:     <span class="amount">8</span>
#:
#: 上一版扫的是纯文本，从 "Complex" 一直切到 "Sort by" 或页尾。2026-09-04 改版
#: 把 "Sort by" 挪到了 "Complex" **前面**，于是这个块一路吃到页尾，把卡片标题里
#: 的单元号当成了计数——"Amsterdam NDSM 63 K2" 里的 63 覆盖掉真正的 8，总数报
#: 成 76 而实际是 22。结论（判 incomplete）碰巧还是对的，理由却是错的。
#:
#: ``name="complexes[]"`` 是表单契约，改它就等于改后端；比版面顺序稳得多。
_RE_COMPLEX_ITEM = re.compile(
    r'<input[^>]*\bvalue="([^"]+)"[^>]*\bname="complexes\[\]"'
    r'.*?<span[^>]*class="[^"]*\bamount\b[^"]*"[^>]*>\s*(\d+)\s*</span>',
    re.S | re.I,
)

#: 详情页。入住日期只在这里有——列表卡片上完全没有日期（2026-09-04 实测：
#: 整页零个日期串，"Start date" 那两次是排序选项）。
DETAIL_URL = f"{BASE_URL}/studios/{{sid}}"

#: 详情页里的两个日期。取前者当 ``available_from``；后者是申请截止，不是入住。
#:
#:     Start date contract   1 October 2026
#:     Respond until         6 September 2026
_RE_START_DATE = re.compile(
    r"Start date contract\s+(\d{1,2}\s+[A-Za-z]+\s+\d{4})", re.I)

#: 每轮最多取几个详情页，以及两次之间的间隔。
#:
#: 这不是正确性旋钮，是流量旋钮：``mstorage._sticky_available_from`` 保证本轮
#: 没问到的房源会沿用上次的真值，所以铺不满一轮不会造成「补一批、抹一批」的
#: 拉锯（H2S 那边为这件事踩过坑，见 holland2stay.py 的四层机制注释）。
#:
#: 站点没有 Cloudflare，但**不是不限速**：2026-09-04 连发两次同一页就吃了一个
#: 403。所以间隔比预算更要紧。
_DETAIL_BUDGET_PER_ROUND = 12
_DETAIL_REQUEST_SPACING = 0.8

#: 进程内缓存：studio id → ISO 日期。开始日期是单元的稳定属性，一个进程里
#: 问一次就够。进程重启后重新铺满，按上面的预算分摊到几轮。
_DETAIL_CACHE: dict[str, str] = {}


def _parse_start_date(page: str) -> "str | None":
    """详情页 → ``YYYY-MM-DD``；认不出返回 None。

    站点写的是 ``1 October 2026`` 这种英文长格式，而库里各 source 统一存 ISO
    （``models.is_sentinel_available_from`` 按前四位判年份，非 ISO 会被误判）。
    """
    m = _RE_START_DATE.search(_plain(page))
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1).strip(), "%d %B %Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


#: 分页链接。``?page=3`` 会**回绕到第 1 页**而不是返回空，所以「翻到空为止」的
#: 循环永远不会停；必须先从这里读出最大页号。
_RE_PAGE_LINK = re.compile(r'class="pagination-link[^"]*"\s+href="[^"]*[?&]page=(\d+)', re.I)


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
    hits = list(_CARD_TAG.finditer(page))
    cards: list[tuple[str, str, str]] = []
    for i, m in enumerate(hits):
        href = _CARD_HREF.search(m.group(0))
        if href is None:
            # class 对上但 href 不是 /studios/<id> —— 结构又变了。跳过而不是
            # 硬猜一个 id：id 猜错会让这条房源和另一条撞 key。
            continue
        end = hits[i + 1].start() if i + 1 < len(hits) else len(page)
        url = html_mod.unescape(href.group(1))
        if url.startswith("/"):
            url = BASE_URL + url
        cards.append((url, href.group(2), page[m.start():end]))
    return cards


def _parse_card(url: str, type_id: str, segment: str,
                term: str = "") -> Optional[Listing]:
    """一张卡片 → Listing；认不出楼盘或价格时返回 None。

    2026-09-04 改版后的结构：

        <a href=".../studios/1943" class="studio is-overview longstay">
          <div class="studio-info top">
            <h3>Amsterdam NDSM<br/>63  K2</h3>        ← 楼盘 / 单元
          </div>
          <div class="studio-info bottom">
            <div class="info-wrap">
              <p><i class="far fa-ruler-combined"></i>24.5 m²</p>
              <p class="info"><i class="fal fa-watch"></i>Long stay &gt; 1 year</p>
              <p class="info"><i class="fal fa-loveseat"></i>Not furnished</p>
            </div>
            <div class="price-wrap"><span class="price">&euro; 1,046<sup>*</sup></span></div>
          </div>

    改版同时改了粒度：以前一张卡片是一个**户型**（"Core Studio"），现在是一个
    **具体单元**（"7 C60"）。所以 name 用单元号，它是用户在站点上看到的那个标识。

    ``info-wrap`` 里那几行按文本模式认，不按图标 class 认。图标是装饰，站点换个
    图标集就全失效；而「N m²」「furnish」「stay」这些模式是内容本身。这跟改版前
    对 ``studio-meta-item`` 的做法是同一条：认内容，别认修饰。
    """
    head = _text(_first(segment, "div", "studio-info top"))
    if not head:
        return None
    # <h3>楼盘<br/>单元</h3> —— _text 把 <br/> 变成空格，按已知楼盘名切
    residence = next((loc for loc, _c in LOCATIONS.values()
                      if head.startswith(loc)), "")
    if not residence:
        logger.warning("Student Experience 出现未登记的楼盘（卡片标题 %r，"
                       "studio %s）", head[:60], type_id)
        return None
    unit = head[len(residence):].strip()

    price = parse_float(_text(_first(segment, "span", "price")))
    if price is None:
        return None

    city = next((c for loc, c in LOCATIONS.values() if loc == residence), "")

    features: list[str] = []

    def _add(key: str, value: object) -> None:
        if value not in (None, ""):
            features.append(f"{key}: {value}")

    area = furnishing = stay = ""
    for raw in _blocks(segment, "div", "info-wrap"):
        for piece in (_text(x) for x in re.split(r"(?i)</p\s*>", raw)):
            if not piece:
                continue
            mm = _RE_AREA.match(piece)
            if mm and not area:
                area = mm.group(1).replace(" ", "")
            elif "furnish" in piece.lower() and not furnishing:
                furnishing = piece
            elif "stay" in piece.lower() and not stay:
                stay = piece

    _add("Building", residence)
    _add("Unit", unit)
    _add("Type", "Studio")
    _add("Area", f"{area} m²" if area else "")
    # 装修档位写进 features 但**不登记为筛选维度**（见模块文档「维度登记」）。
    _add("Finishing note", furnishing)
    # 长租卡片自带期限（"Long stay > 1 year" / "Maximum stay until June 30, 2027"）；
    # 短租那条线的期限由调用方按学期档传进来。两者都写同一个键。
    _add("Length of stay", term or stay)

    return Listing(
        id=f"se_{type_id}",
        name=f"{residence} — {unit}".strip().strip("—").strip(),
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
    known = {name for name, _ in LOCATIONS.values()}
    return {name.strip(): int(n)
            for name, n in _RE_COMPLEX_ITEM.findall(page)
            if name.strip() in known}


def _last_page(page: str) -> int:
    """分页里的最大页号；没有分页返回 1。"""
    return max((int(n) for n in _RE_PAGE_LINK.findall(page)), default=1)


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
        """取一个页面。**传输层异常一律包成 ScrapeNetworkError。**

        包装不是为了好看，是为了让代理故障被认出来。dispatcher 只在
        ``except ScrapeNetworkError`` 那一支里调 ``is_proxy_error``（见
        scrapers/__init__.py:272）；裸的 ``curl_cffi.ProxyError`` 会掉进后面的
        通用 ``except Exception``，被记成「未预期异常」，于是**永远不会**触发代理
        冷却，本 source 就只能干等别的 source 去发现代理坏了。

        2026-09-02 生产实测正是这样：代理 402 欠费，同一轮里 xior 报「代理已确认
        故障并进入冷却」，本 source 报的却是「未预期异常，已隔离该任务」。

        起作用的是 ``from e``——``is_proxy_error`` 走 ``_exception_chain_text``，
        整条 ``__cause__`` 链都在它的搜索范围里，"CONNECT tunnel failed" /
        curl (56) 这些特征串从原始异常那里就能读到。消息里再抄一遍原文只是为了
        日志好读，**不是**识别的前提（照抄与否对 ``is_proxy_error`` 无差别，
        变异测试证实了这一点）。裸抛才是真的会漏——那时根本走不到这一支。
        """
        try:
            resp = session.get(url, params=params, timeout=30,
                               headers={"Accept-Language": "en-US,en;q=0.9"})
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:
            raise ScrapeNetworkError(
                f"Student Experience {url} 请求失败: {type(e).__name__}: {e}") from e
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

            # 长租分页。上限从分页控件读，**不能**「翻到空为止」——超出范围的
            # 页号（?page=3）返回的是第 1 页而不是空页，那种循环停不下来。
            last = _last_page(long_page)
            for n in range(2, last + 1):
                long_cards += _collect(
                    self._get(session, STUDIOS_URL,
                              {"los": "longstay", "page": n}))

            expected = sum(counts.values())
            if expected > 0 and long_cards == 0:
                # 计数说有货、卡片一张都没解析出来 → 卡片结构变了。
                logger.warning(
                    "Student Experience 长租计数为 %d 但解析出 0 张卡片，"
                    "本轮标记不完整（计数：%s）", expected, counts)
                return [], False
            if long_cards < expected:
                # 数得出来、却没拿全。多半是分页没翻到底（站点换了分页控件），
                # 也可能是某些卡片解析失败。这两种都不该当成「其余的下架了」。
                logger.warning(
                    "Student Experience 长租计数 %d 但只解析出 %d 张（共 %d 页），"
                    "本轮标记不完整（计数：%s）",
                    expected, long_cards, last, counts)
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

            # ── 入住日期：只在详情页有，逐条补 ──
            #
            # 失败**不**影响本轮判定：房源本身是完整的，只是少一个字段；而
            # mstorage._sticky_available_from 会让已经问到过的房源沿用旧值。
            # 把这里的失败升格成 incomplete，等于让一个可选字段有权否决整轮。
            self._fill_start_dates(session, listings)

        logger.info("Student Experience 共抓取 %d 条户型（长租计数：%s）",
                    len(listings), counts)
        return listings, self._complete

    def _fill_start_dates(self, session, listings: list[Listing]) -> None:
        """给 listing 补 ``available_from``。缓存命中的不发请求，其余按预算取。"""
        budget = _DETAIL_BUDGET_PER_ROUND
        fetched = failed = 0
        for item in listings:
            sid = item.id[3:] if item.id.startswith("se_") else item.id
            if sid in _DETAIL_CACHE:
                item.available_from = _DETAIL_CACHE[sid]
                continue
            if budget <= 0:
                continue
            budget -= 1
            if fetched:
                time.sleep(_DETAIL_REQUEST_SPACING)
            try:
                page = self._get(session, DETAIL_URL.format(sid=sid))
            except ScrapeNetworkError as e:
                # 详情页限速/抖动很常见（实测连发两次就吃 403）。记一次、继续，
                # 下一轮再补——不抛，否则一个可选字段能让整轮抓取失败。
                failed += 1
                logger.debug("Student Experience 详情页 %s 取失败: %s", sid, e)
                continue
            fetched += 1
            date = _parse_start_date(page)
            if date:
                _DETAIL_CACHE[sid] = date
                item.available_from = date
            else:
                # 页面拿到了却认不出日期 —— 这是结构变了，值得说出来；
                # 但同样不升格成 incomplete。
                logger.warning(
                    "Student Experience 详情页 %s 里读不到 Start date contract", sid)
        if fetched or failed:
            logger.info(
                "Student Experience 入住日期：本轮取 %d 条（失败 %d），"
                "缓存 %d 条，仍缺 %d 条",
                fetched, failed, len(_DETAIL_CACHE),
                sum(1 for x in listings if not x.available_from))

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
