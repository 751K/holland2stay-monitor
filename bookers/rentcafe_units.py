"""bookers/rentcafe_units.py — 解析 RENTCafe「选房」步骤的单元列表

`oleapplication.aspx?stepname=Apartments` 上每个可订单元一个「Reserve this room」
按钮。按钮名字唬人，实际动作是**选中该单元并跳到 ApplicantInfo**，不是当场锁房
（2026-08-03 用真实账号实测）。

按钮长这样（实测抄录，已折行）::

    <button class="btn UnitSelect btn btn-primary" name="1.S127" id="1.S127"
      onclick="ContinueClick('398336','1111515','185795','16-8-2026','',
        'oleapplication.aspx?myLeaseCafeType=2&stepname=ApplicantInfo&FromUnitSelection=1',
        '0','0','648','3281','1','16-8-2026','1-11-2026','','0','0','0','0', …)">

第一个参数就是 **unitId**，而抓取侧的 listing id 正是 ``xr_<unitId>``
（例：`398336` ↔ `xr_398336`）。两侧标识符天然对齐，选房不需要额外的映射表——
这是这个平台少有的好性质，值得专门利用。

本模块只做解析，不发请求：解析规则能脱离网络和账号测试，而这正是最容易因为
上游改版而悄悄失效的部分。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# onclick="ContinueClick('a','b','c',…)" —— 只取参数串，逐个再拆
_CONTINUE_CLICK_RE = re.compile(
    r"ContinueClick\s*\(\s*(?P<args>.*?)\s*\)\s*[;\"']", re.S,
)
# 带 UnitSelect 类的按钮整体（用于把 name/id 和 onclick 关联起来）
_UNIT_BUTTON_RE = re.compile(
    r"<button[^>]*\bclass=[\"'][^\"']*\bUnitSelect\b[^\"']*[\"'][^>]*>", re.I,
)
_ATTR_RE = re.compile(r"\b(?P<k>name|id)=[\"'](?P<v>[^\"']*)[\"']", re.I)
_ARG_RE = re.compile(r"'([^']*)'")


@dataclass(frozen=True, slots=True)
class UnitOption:
    """选房页上的一个可订单元。"""

    unit_id: str          # ContinueClick 第 1 参数；== 抓取侧 xr_<id> 的 id
    floor_plan_id: str    # 第 2 参数
    property_id: str      # 第 3 参数
    available_date: str   # 第 4 参数，d-m-yyyy
    next_url: str         # 第 6 参数：选中后要跳的相对 URL
    label: str = ""       # 按钮的 name/id，形如 "1.S127"（房号，给日志用）

    @property
    def listing_id(self) -> str:
        """抓取侧的 listing id，用来和 ``Listing.id`` 直接比对。"""
        return f"xr_{self.unit_id}"


def parse_unit_options(html: str) -> list[UnitOption]:
    """从选房页 HTML 解析出全部可订单元。

    解析失败一律返回空列表而不是抛异常——上游改版时应该表现为「没找到可订
    单元」（调用方会当作本次抢不到，安全地放弃），而不是让整个预订链路崩掉。
    """
    if not html:
        return []
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
            label=(attrs.get("name") or attrs.get("id") or "").strip(),
        ))
    return out


def find_unit(html: str, listing_id: str) -> UnitOption | None:
    """在选房页里找到与某条 listing 对应的单元。

    ``listing_id`` 传抓取侧的 ``xr_<id>``（也接受裸 id）。找不到返回 None——
    **不要退而求其次选一个「差不多」的单元**：用户是冲着某个具体房号来的，
    抢到别的等于替他做了个他没同意的决定。
    """
    wanted = (listing_id or "").strip()
    if wanted.startswith("xr_"):
        wanted = wanted[3:]
    if not wanted:
        return None
    for opt in parse_unit_options(html):
        if opt.unit_id == wanted:
            return opt
    return None
