"""bookers/rentcafe_units.py — 解析 RENTCafe 的「选中某个单元」按钮

同一套 RENTCafe，两个平台把「选房」放在流程的**不同位置**，按钮也不是同一个：

===========  ==============================  ===================================
平台          按钮 / 处理器                    在哪一页
===========  ==============================  ===================================
Xior         ``ContinueClick(...)``          登录后的 ``oleapplication.aspx
                                             ?stepname=Apartments``
OurDomain    ``ApplyNowClick(...)``          抓取侧那张 availableunits 表
                                             （``rcLoadContent.ashx``）
===========  ==============================  ===================================

两者产出同一种东西——一组「哪个单元 / 哪个户型 / 哪个 property / 哪天入住 /
下一步去哪」——所以统一解析成 :class:`UnitOption`，让 booker 那边只有一条
路径。

Xior 的按钮（实测抄录，已折行）::

    <button class="btn UnitSelect btn btn-primary" name="1.S127" id="1.S127"
      onclick="ContinueClick('398336','1111515','185795','16-8-2026','',
        'oleapplication.aspx?myLeaseCafeType=2&stepname=ApplicantInfo&FromUnitSelection=1',
        '0','0','648','3281','1','16-8-2026','1-11-2026','','0','0','0','0', …)">

OurDomain 的按钮（2026-08-04 实测抄录）::

    <input type="button" value="Book now"
      onclick="ApplyNowClick('211053','1113962','184283','14-9-2026',
        'termsandotheritems.aspx')" />

参数**位置不同**：Xior 的 nextUrl 在第 6 位，OurDomain 在第 5 位；OurDomain
没有 SchoolId（那是 Xior 学生房专有的）。这就是两个解析函数而不是一个带开关
的原因——位置写错不会报错，只会带着一组错参数去提交。

两个平台的 unit id 都和抓取侧的 listing id 天然对齐（``xr_398336`` /
``od_211053``），选房不需要额外的映射表。

本模块只做解析，不发请求：解析规则能脱离网络和账号测试，而这正是最容易因为
上游改版而悄悄失效的部分。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from html import unescape as _unescape

logger = logging.getLogger(__name__)

# onclick="ContinueClick('a','b','c',…)" —— 只取参数串，逐个再拆
_CONTINUE_CLICK_RE = re.compile(
    r"ContinueClick\s*\(\s*(?P<args>.*?)\s*\)\s*[;\"']", re.S,
)
# 带 UnitSelect 类的按钮整体（用于把 name/id 和 onclick 关联起来）
_UNIT_BUTTON_RE = re.compile(
    r"<button[^>]*\bclass=[\"'][^\"']*\bUnitSelect\b[^\"']*[\"'][^>]*>", re.I,
)
#: OurDomain 的「Book now」。它不在按钮的 class 上做标记，只能认 onclick 本身；
#: 也不一定是 ``<button>``（实测是 ``<input type="button">``）。
_APPLY_NOW_CLICK_RE = re.compile(
    r"ApplyNowClick\s*\(\s*(?P<args>.*?)\s*\)", re.S,
)
_APPLY_NOW_ROW_RE = re.compile(
    r"<tr\b(?=[^>]*\bid=[\"']unitrow_(?P<uid>\d+)[\"'])[^>]*>(?P<body>.*?)</tr>",
    re.I | re.S,
)
_ATTR_RE = re.compile(r"\b(?P<k>name|id)=[\"'](?P<v>[^\"']*)[\"']", re.I)
_ARG_RE = re.compile(r"'([^']*)'")


@dataclass(frozen=True, slots=True)
class UnitOption:
    """一个可选中的具体单元，两个平台共用。"""

    unit_id: str          # 第 1 参数；== 抓取侧 <前缀>_<id> 里的 id
    floor_plan_id: str    # 第 2 参数
    property_id: str      # 第 3 参数
    available_date: str   # 第 4 参数，d-m-yyyy
    #: 选中后要跳/提交到的相对 URL。Xior 在第 6 参数（``oleapplication.aspx
    #: ?stepname=ApplicantInfo``），OurDomain 在第 5 参数（``termsandotheritems
    #: .aspx``）。
    next_url: str
    #: Xior 第 9 参数：学生类别 SchoolId（实测 648=Dutch）。申请表上的
    #: ``drpSchool`` 是必填下拉，值就是它——从单元推出来，不用让用户填。
    #: OurDomain 不是学生公寓，没有这个参数，留空。
    school_id: str = ""
    label: str = ""       # 房号，形如 "1.S127" / "#6031"（给日志用）
    #: 抓取侧的 id 前缀。**必须和对应 scraper 的 ``ID_PREFIX`` 一致**——
    #: 它是 :attr:`listing_id` 唯一的真相来源，写错会让 ``find_unit`` 恒
    #: 找不到单元，而那条路径的表现是「已被他人选走」，看不出是前缀错了。
    id_prefix: str = "xr_"

    @property
    def listing_id(self) -> str:
        """抓取侧的 listing id，用来和 ``Listing.id`` 直接比对。"""
        return f"{self.id_prefix}{self.unit_id}"


def parse_unit_options(html: str) -> list[UnitOption]:
    """从选房页 HTML 解析出全部可订单元。

    解析失败一律返回空列表而不是抛异常——上游改版时应该表现为「没找到可订
    单元」（调用方会当作本次抢不到，安全地放弃），而不是让整个预订链路崩掉。
    """
    if not html:
        return []
    # 实测页面里 onclick 内部的引号是 HTML 实体：
    #   onclick="ContinueClick(&#39;398336&#39;,&#39;1111515&#39;,…)"
    # 上面文档里那段是解码后的样子。2026-08-03 踩过：正则按真引号写，结果
    # 页面上 21 个单元一个都没解析出来，流程报「已被他人选走」——**明明单元
    # 就在页面上**。先整体解码再匹配。
    html = _unescape(html)
    out: list[UnitOption] = []
    for m in _UNIT_BUTTON_RE.finditer(html):
        tag = m.group(0)
        attrs = {a.group("k").lower(): a.group("v") for a in _ATTR_RE.finditer(tag)}
        cc = _CONTINUE_CLICK_RE.search(tag)
        if not cc:
            continue
        args = _ARG_RE.findall(cc.group("args"))
        if len(args) < 6:
            logger.debug("ContinueClick 参数不足 6 个，跳过：%s", args[:6])
            continue
        unit_id = (args[0] or "").strip()
        if not unit_id:
            continue
        out.append(UnitOption(
            unit_id=unit_id,
            floor_plan_id=(args[1] or "").strip(),
            property_id=(args[2] or "").strip(),
            available_date=(args[3] or "").strip(),
            next_url=(args[5] or "").strip(),
            school_id=(args[8] or "").strip() if len(args) > 8 else "",
            label=(attrs.get("name") or attrs.get("id") or "").strip(),
        ))
    return out


def parse_apply_now_options(html: str) -> list[UnitOption]:
    """从 OurDomain 的 availableunits 表解析出全部可订单元。

    数据源是抓取侧那张表（``rcLoadContent.ashx?contentclass=availableunits``），
    不是某个 aspx 页——OurDomain 的选房动作发生在申请流程**开始之前**。

    为什么按 ``<tr>`` 逐行匹配而不是全局找 ``ApplyNowClick``：``unitrow_<id>``
    是这张表里唯一权威的单元 id，用它交叉核对 onclick 的第 1 参数，能在上游哪天
    调整参数顺序时**当场发现**，而不是默默拿着错位的参数去提交。

    解析失败一律返回空列表，理由同 :func:`parse_unit_options`。
    """
    if not html:
        return []
    html = _unescape(html)
    out: list[UnitOption] = []
    for row in _APPLY_NOW_ROW_RE.finditer(html):
        body = row.group("body")
        cc = _APPLY_NOW_CLICK_RE.search(body)
        if not cc:
            continue      # 该行没有「Book now」= 这个单元当前不可订
        args = _ARG_RE.findall(cc.group("args"))
        if len(args) < 5:
            logger.debug("ApplyNowClick 参数不足 5 个，跳过：%s", args[:5])
            continue
        unit_id = (args[0] or "").strip()
        row_id = row.group("uid")
        if unit_id != row_id:
            # 参数顺序变了，或者这一行的按钮属于别的单元。两种都不能猜。
            logger.warning(
                "OurDomain unitrow_%s 的 ApplyNowClick 第 1 参数是 %r，对不上——"
                "参数顺序可能已变，跳过该单元以免提交错的房号。", row_id, unit_id,
            )
            continue
        apt = re.search(r">\s*(#[^<]{1,20})<", body)
        out.append(UnitOption(
            unit_id=unit_id,
            floor_plan_id=(args[1] or "").strip(),
            property_id=(args[2] or "").strip(),
            available_date=(args[3] or "").strip(),
            next_url=(args[4] or "").strip(),
            school_id="",                      # OurDomain 不是学生公寓
            label=(apt.group(1).strip() if apt else f"#{unit_id}"),
            id_prefix="od_",
        ))
    return out


def find_unit(
    html: str,
    listing_id: str,
    *,
    id_prefix: str = "xr_",
    parser=parse_unit_options,
) -> UnitOption | None:
    """在页面里找到与某条 listing 对应的单元。

    ``listing_id`` 传抓取侧的 ``<前缀>_<id>``（也接受裸 id）。找不到返回 None——
    **不要退而求其次选一个「差不多」的单元**：用户是冲着某个具体房号来的，
    抢到别的等于替他做了个他没同意的决定。

    ``id_prefix`` / ``parser`` 由调用方按平台传。**不做「自动识别前缀」**：
    ``od_`` 的 listing 配上 Xior 的解析器只会静默找不到，表现成「已被他人
    选走」——把平台身份显式传进来，错了会当场炸，不会变成一条假的竞争失败。
    """
    wanted = (listing_id or "").strip()
    if id_prefix and wanted.startswith(id_prefix):
        wanted = wanted[len(id_prefix):]
    if not wanted:
        return None
    for opt in parser(html):
        if opt.unit_id == wanted:
            return opt
    return None
