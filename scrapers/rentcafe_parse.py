"""RENTCafe 页面解析：floorplan 列表、单元表格、单元状态。

OurDomain / OurCampus 的抓取器和 RentCafe 预订器（bookers/rentcafe.py）都靠它读
同一种页面。「这个单元能不能订」的判据必须两边一致，所以只能有一份实现。

从 scrapers/ourdomain.py 拆出；那边保留旧的名字作别名。纯函数，不触网、无状态。
"""
from __future__ import annotations

import logging
import re
from datetime import date
from html import unescape
from typing import Optional

logger = logging.getLogger(__name__)


def _extract_floorplan_ids(html: str) -> list[str]:
    # 有 two 种 RENTCafe FP id 格式：
    #   subPointerId=NNNN          — photo gallery onclick（Diemen 在用）
    #   myFloorPlanId=NNNN         — Get Notified / Contact Us 链接（South-East 在用）
    ids = re.findall(r"subPointerId=(\d+)", html)
    if not ids:
        ids = re.findall(r"myFloorPlanId=(\d+)", html)
    return list(dict.fromkeys(ids))


def _extract_units(html: str) -> list[dict]:
    units: list[dict] = []
    for row in re.finditer(
        r"<tr\b(?=[^>]*\bid=[\"']unitrow_(\d+)[\"'])[^>]*>.*?</tr>",
        html,
        re.IGNORECASE | re.DOTALL,
    ):
        unit = _extract_unit(row.group(0), row.group(1))
        if unit:
            units.append(unit)
    return units


def _extract_unit(row_html: str, unit_id: str) -> Optional[dict]:
    """
    从单条 unitrow 提取 unit 数据。selenium-id 用 Diemen 主题命名，
    data-label 作为兜底（South-East 等其它 RentCafe 主题）。

    每个字段都传一组 label fallback——data-label 是表格的"用户可见列标题"，
    跨主题稳定（Sq.M. / Apartment / Rent / Deposit / Amenities / Availability）。
    """
    idx_match = re.search(
        r"data-selenium-id=[\"']urow(\d+)[\"']",
        row_html,
        re.IGNORECASE,
    )
    idx = idx_match.group(1) if idx_match else "1"

    apt = _cell_text(row_html, f"Apt{idx}", labels=("Apartment", "Apt", "Unit"))
    if not apt:
        logger.warning("OurDomain 单元缺少 Apt%s: unit_id=%s", idx, unit_id)
        return None

    # Amenity / Floor info：selenium-id Amenity{idx}，fallback 用 data-label
    # "Amenities" / "Floor"（不同主题可能命名其一）
    amenity_html = _cell_html(
        row_html, f"Amenity{idx}", labels=("Amenities", "Floor", "Details"),
    )
    detail_labels = _label_texts(amenity_html)
    detail = ", ".join(detail_labels)
    floor = parse_ourdomain_floor(detail)
    avail_date = _extract_apply_date(row_html)

    sqft = _cell_text(row_html, f"SqFt{idx}", labels=("Sq.M.", "Sq.Ft.", "Size", "Area"))
    rent = _cell_text(row_html, f"Rent{idx}", labels=("Rent",))
    deposit = _cell_text(row_html, f"Deposit{idx}", labels=("Deposit",))
    avail_html = _cell_html(
        row_html, f"AvailDate{idx}", labels=("Availability", "Available", "Apply"),
    )

    # 关键字段全空时打一条 WARNING，方便日后回看哪些主题需要再加 label fallback
    if not sqft and not detail and not rent:
        logger.warning(
            "OurDomain 单元 %s 解析后核心字段全空——可能是新主题 / 新字段名，"
            "row_html 前 300: %s",
            unit_id, row_html[:300],
        )

    return {
        "unit_id": unit_id,
        "apt": apt,
        "sqft": sqft,
        "rent": rent,
        "deposit": deposit,
        "detail": detail,
        "floor": floor,
        # 传整行：可下单按钮在行里，不在 availability 那一格
        "status": _extract_status(avail_html, row_html),
        "avail_date": avail_date,
        "fp_ids": [],
    }


def _cell_html(row_html: str, selenium_id: str, *, labels: tuple[str, ...] = ()) -> str:
    """
    抓单元 cell 的 HTML，**多策略容错**：

    1. `data-selenium-id="<selenium_id>"` 精确匹配（Diemen 用这套）
    2. `data-label="<label>"` 任一匹配（South-East 等其它 RentCafe 主题
       可能用 data-label 而 selenium-id 不同；data-label 是用户可见标签，
       跨主题更稳定）

    任一策略命中即返回；都没命中返回空串。

    为什么需要兜底
    --------------
    OurDomain 的两个楼盘用了不同的 RentCafe 主题。Diemen 的 row 是
    `<td data-selenium-id="SqFt1" data-label="Sq.M.">27</td>`，South-East
    实测 SqFt cell 的 selenium-id 跟 Diemen 不同。data-label 在两套主题
    里都稳定存在，作为兜底胜率更高。
    """
    # 策略 1：selenium-id 精确匹配
    pattern = (
        r"<(?P<tag>th|td)\b"
        rf"(?=[^>]*data-selenium-id=[\"']{re.escape(selenium_id)}[\"'])"
        r"[^>]*>(?P<body>.*?)</(?P=tag)>"
    )
    m = re.search(pattern, row_html, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group("body")

    # 策略 2：data-label 兜底（允许任一 label 命中）
    for label in labels:
        pattern2 = (
            r"<(?P<tag>th|td)\b"
            rf"(?=[^>]*data-label=[\"']?{re.escape(label)}[\"']?)"
            r"[^>]*>(?P<body>.*?)</(?P=tag)>"
        )
        m = re.search(pattern2, row_html, re.IGNORECASE | re.DOTALL)
        if m:
            return m.group("body")

    return ""


def _cell_text(row_html: str, selenium_id: str, *, labels: tuple[str, ...] = ()) -> str:
    return _strip_html(_cell_html(row_html, selenium_id, labels=labels))


def _strip_html(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", unescape(value)).strip()


def _label_texts(html: str) -> list[str]:
    labels: list[str] = []
    for m in re.finditer(r"<label\b[^>]*>(.*?)</label>", html, re.IGNORECASE | re.DOTALL):
        text = _strip_html(m.group(1))
        if not text:
            continue
        lower = text.lower()
        if lower == "max-rent" or "prices and special offers" in lower:
            continue
        labels.append(text)
    return labels


#: 可下单控件。RENTCafe 的单元行里，能不能订**由这个按钮决定**，不由状态格的
#: 样式类决定。三个特征任一命中即可——站点换主题时类名和文案都可能变，但
#: ApplyNowClick 这个回调是下单流程的入口，最不容易动。
_BOOKABLE_CONTROL_RE = re.compile(
    r"UnitSelect|ApplyNowClick|value\s*=\s*[\"']\s*Book Now\s*[\"']",
    re.IGNORECASE,
)


#: 被禁用的按钮不算数（目前实测没出现过，但不能指望它永远不出现）。
_DISABLED_CONTROL_RE = re.compile(r"<input\b[^>]*\bdisabled\b", re.IGNORECASE)


#: 见过并且已经想清楚含义的状态格样式。没见过的形态要告警，不能默默归类。
_KNOWN_AVAIL_CLASSES = ("success", "warning", "muted", "danger")


#: 下单按钮上的文字。**同一个按钮、同一个 ApplyNowClick，文字决定点下去是什么**：
#: ``Book Now`` 是先到先得，``Join Lottery`` 是进抽签池。OurCampus 两种并存
#: （2026-09-15 首次见到后者，见 ``_extract_status``）。
_CONTROL_LABEL_RE = re.compile(
    r"<input\b(?=[^>]*(?:UnitSelect|ApplyNowClick))[^>]*?\bvalue\s*=\s*[\"']([^\"']*)[\"']",
    re.IGNORECASE,
)


_LOTTERY_LABEL_WORDS = ("lottery", "loting", "loterij")


#: 见过并且确认是「直接订」的按钮文字。其余文字放行并告警。
_KNOWN_BOOK_LABELS = ("book now",)


def _has_bookable_control(row_html: str) -> bool:
    if not row_html:
        return False
    if _DISABLED_CONTROL_RE.search(row_html):
        return False
    return bool(_BOOKABLE_CONTROL_RE.search(row_html))


def _control_label(row_html: str) -> str:
    """下单按钮上的文字（小写、去空白）；没有按钮或按钮不带 value 返回空串。"""
    m = _CONTROL_LABEL_RE.search(row_html or "")
    return " ".join(m.group(1).split()).lower() if m else ""


def _extract_status(avail_html: str, row_html: str = "") -> str:
    """判定单元状态。**判据是有没有可下单的按钮**，不是状态格的样式类。

    为什么改（2026-08-27）
    ---------------------
    原实现只看状态格里第一个 span 的 class：``success`` → 可订，``warning`` →
    抽签，**其余一律 Occupied**。而 OurCampus 当天列出的单元是这样的：

        <span class='muted'>31-8-2026</span>
        <input class='UnitSelect btn btn-primary' value='Book Now'
               onclick='ApplyNowClick("457252",…,"10-9-2026",…)'>

    ``muted`` 不在判据里，于是掉进兜底判成 Occupied——而那个 Book Now 按钮
    和当天判为 Available 的单元**一模一样**。快照实测：34 个 UnitSelect 按钮
    形态完全一致、0 个 disabled，状态格 21 个 success + 13 个 muted，正好
    一一对应。也就是说 ``muted`` 的含义是「**某日起**可订」，不是「已出租」。

    后果：那三套房源挂了约一小时（11:22–12:19）带着能点的按钮，我们报成
    Occupied，一条通知都没发；再看时已经没了。

    这和 2026-08-25 Xior 那次是同一个错误的两个方向：那次「类名说能订、文字
    写着 Rented Out」判宽了，这次「按钮明明能订、类名不认识」判严了。共同点
    是**判据看的是外观标记，而不是那个决定「能不能订」的东西**。

    ``row_html`` 缺省时退回旧的类名判据——老调用方和单元测试还按单格传参，
    不能因为拿不到整行就把所有单元判成不可订。

    按钮文字（2026-09-15）
    ---------------------
    上面那次修正把「有按钮」当成了「能直接订」。OurCampus 当天挂出的行::

        <span class='muted'>30-9-2026</span>
        <input class='UnitSelect btn btn-primary' value ='Join Lottery'
               onclick='return ApplyNowClick("456556",…,"8-10-2026",…)'>

    和 08-27 的 Book Now 行**只差按钮上的字**：同样的类名、同样的回调、同样
    的 muted 日期格。于是 #2301 和下午那批全都以「Available to book」推了
    出去——告诉用户手快就能订到，而实际是进抽签池。

    所以按钮的**存在**决定「能不能点」，按钮的**文字**决定「点了是什么」。
    二者缺一都会判错，这是同一个判据被拆成两半、只实现了一半。
    """
    m = re.search(
        r"<span\b[^>]*class=[\"']([^\"']*)[\"'][^>]*>(.*?)</span>",
        avail_html,
        re.IGNORECASE | re.DOTALL,
    )
    classes = m.group(1).lower() if m else ""
    text = _strip_html(m.group(2)).lower() if m else ""

    # 抽签是独立的一档：它也有按钮，但点下去是进池子不是直接订到房。
    if "warning" in classes or "wait" in text:
        return "Available in lottery"

    # 两个独立的可订证据，取其一即可：
    #   ① 状态格明确写着可订（老主题就靠这个，且有些行不带按钮）
    #   ② 行里有可下单按钮（muted + 日期那种就靠这个）
    # 要求两者同时成立会把 ① 那类主题全判成不可订——合成 fixture
    # test_southeast_style_row_extracts_all_fields 当场抓到过这个回归。
    explicit = "success" in classes or text == "available"
    bookable = _has_bookable_control(row_html)
    label = _control_label(row_html) if bookable else ""

    # 按钮文字写着抽签，就是抽签——不管状态格是什么样式。
    if any(w in label for w in _LOTTERY_LABEL_WORDS):
        return "Available in lottery"

    if bookable and label and label not in _KNOWN_BOOK_LABELS:
        # 放行并告警：Join Lottery 当初就是一个「没见过的文字」被默默当成了
        # Book Now。放行是因为漏报比误报贵；告警是为了下一个新文字别再默默过去。
        logger.warning(
            "OurDomain/OurCampus 下单按钮出现没见过的文字 %r——按可订放行。"
            "若它其实是抽签 / 等位 / 订不到，会误报一条可订房源", label[:40],
        )

    if explicit or bookable:
        if bookable and not explicit and classes and not any(
            k in classes for k in _KNOWN_AVAIL_CLASSES
        ):
            # 放行并告警，与 scrapers/xior.py 对未知按钮文字的处理一致：
            # 漏报真房源比误报贵，但不能再让「没见过」悄悄变成 Occupied。
            logger.warning(
                "OurDomain/OurCampus 状态格出现没见过的样式 %r（文本 %r），"
                "但行内有可下单按钮——按可订放行。若它其实代表订不到，"
                "会误报一条房源", classes[:60], text[:40],
            )
        return "Available to book"
    return "Occupied"


def _extract_apply_date(row_html: str) -> Optional[str]:
    m = re.search(
        r"ApplyNowClick\([^)]*?[\"'](\d{1,2}-\d{1,2}-\d{4})[\"']",
        row_html,
        re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return None
    return _normalize_date(m.group(1))


def _normalize_date(value: str) -> Optional[str]:
    value = value.strip()
    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})-(\d{4})", value)
    if m:
        day, month, year = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return f"{year:04d}-{month:02d}-{day:02d}"
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", value)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return None


def parse_ourdomain_floor(detail: str) -> Optional[int]:
    """Extract the lowest floor number from an OurDomain amenity label."""
    if not detail:
        return None
    lower = detail.lower()
    if "ground" in lower:
        return 0
    m = re.search(r"Floor\s*(\d+)", detail, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _next_month_first() -> str:
    today = date.today()
    year = today.year + (today.month // 12)
    month = (today.month % 12) + 1
    return f"{year:04d}-{month:02d}-01"
