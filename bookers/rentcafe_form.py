"""bookers/rentcafe_form.py — 解析 Applicant Info 页，拿到**真实**字段名

为什么必须由页面驱动
--------------------
2026-08-03 实测发现：上一版把 15 个字段名全写错了，命中 0 个。原因是那些名字
是从页面**可见标签**抄的（「First Name」→ ``FirstName``），而不是从 HTML 的
``name`` 属性抄的（真名是 ``ProspectFirstName``）。后果不是报错，是**默默提交
一份什么都没填上的申请**——服务端照单全收，静默丢弃。

更要命的是，有一批字段名里嵌着 **prospect id**（每份申请都不同）::

    PrefMoveinDate2057105    drpGender2057105     ProspectDOB2057105
    Currentaddr2057105Addr1  OwnerShipType2057105

这类名字**根本不可能硬编码**。下拉框同理：国家有 244 个选项，值是 RENTCafe
的内部数字 id（Netherlands=2、China=49），不同物业未必一致。

所以这一层只做一件事：把页面读成「有哪些字段、每个下拉有哪些合法取值」，
让映射层按名字去查，而不是凭记忆去写。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from html import unescape

logger = logging.getLogger(__name__)

_FORM_RE = re.compile(
    r'<form[^>]*\bid="ApplicantInformation".*?</form>', re.S | re.I,
)
_INPUT_RE = re.compile(r"<(input|select|textarea)\b[^>]*>", re.I)
_NAME_RE = re.compile(r'\bname="([^"]+)"', re.I)
_SELECT_RE = re.compile(
    r'<select\b[^>]*\bname="(?P<name>[^"]+)"[^>]*>(?P<body>.*?)</select>',
    re.S | re.I,
)
_OPTION_RE = re.compile(r"<option\b[^>]*>.*?</option>", re.S | re.I)
_LABEL_RE = re.compile(r"<label\b[^>]*>.*?</label>", re.S | re.I)
_VALUE_RE = re.compile(r'\bvalue="([^"]*)"', re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_PROSPECT_ID_RE = re.compile(
    r'<input[^>]*\bname="ProspectI[dD]"[^>]*\bvalue="(\d+)"', re.I,
)


@dataclass(frozen=True, slots=True)
class ApplicantForm:
    """一张具体的 Applicant Info 表单。"""

    prospect_id: str
    #: 表单里出现过的全部字段名
    names: frozenset[str]
    #: 下拉框 ``{字段名: {选项文字: 提交值}}``
    selects: dict[str, dict[str, str]]
    #: ``disabled`` 的字段。jQuery 不序列化它们，我们也不该发——那是服务端
    #: 自己算的值（``LeaseTerm`` 就是这样）。
    disabled: frozenset[str]
    #: ``{字段名: 页面上显示的标签}``。物业会**改标签复用字段**——Xior 就把
    #: 地址第二行 ``Currentaddr{pid}Addr2`` 的标签改成了「University」。光看
    #: 字段名会把大学名填进地址栏，看标签才知道这一格实际在要什么。
    labels: dict[str, str]

    def has(self, name: str) -> bool:
        return name in self.names and name not in self.disabled

    def label_of(self, name: str) -> str:
        return self.labels.get(name, "")

    def pid(self, template: str) -> str:
        """``"drpGender{pid}"`` → ``"drpGender2057105"``。"""
        return template.format(pid=self.prospect_id)

    def option_value(self, field: str, label: str) -> str:
        """按选项**文字**取提交值；找不到返回空串。

        存显示名而不是数字 id，是因为 id 是 RENTCafe 的内部主键，换个物业或
        一次改版就可能变；显示名稳定得多，每次提交前现查即可。
        """
        opts = self.selects.get(field) or {}
        want = (label or "").strip().casefold()
        if not want:
            return ""
        for text, value in opts.items():
            if text.casefold() == want:
                return value
        return ""


def parse_applicant_form(html: str) -> ApplicantForm | None:
    """解析 Applicant Info 页；不是这张表就返回 None。

    返回 None 而不是抛异常：调用方（booker）要把它转成一条「页面结构变了」
    的失败结果报给用户，而不是让整条预订链路崩掉。
    """
    m = _FORM_RE.search(html or "")
    if not m:
        return None
    body = unescape(m.group(0))

    pid_m = _PROSPECT_ID_RE.search(body)
    if not pid_m:
        logger.warning("Applicant Info 页里找不到 ProspectId，字段名无法定位")
        return None

    names: set[str] = set()
    disabled: set[str] = set()
    labels: dict[str, str] = {}
    # label 的 for 属性**对不上** input 的 id（实测 for="CurrentAddress22057105"
    # 而 id="Currentaddr2057105Addr2"），只能按位置取最近的前置 label。
    label_spans = [
        (m.end(), _TAG_RE.sub("", m.group(0)).replace("*", "").strip())
        for m in _LABEL_RE.finditer(body)
    ]
    for tag_m in _INPUT_RE.finditer(body):
        tag = tag_m.group(0)
        name_m = _NAME_RE.search(tag)
        if not name_m:
            continue
        name = name_m.group(1)
        names.add(name)
        if re.search(r"\bdisabled\b", tag, re.I):
            disabled.add(name)
        prior = [text for end, text in label_spans if end <= tag_m.start() and text]
        if prior:
            labels[name] = prior[-1]

    selects: dict[str, dict[str, str]] = {}
    for sel in _SELECT_RE.finditer(body):
        opts: dict[str, str] = {}
        for opt in _OPTION_RE.finditer(sel.group("body")):
            raw = opt.group(0)
            text = _TAG_RE.sub("", raw).strip()
            val_m = _VALUE_RE.search(raw)
            if text:
                opts[text] = val_m.group(1) if val_m else text
        selects[sel.group("name")] = opts

    return ApplicantForm(
        prospect_id=pid_m.group(1),
        names=frozenset(names),
        selects=selects,
        disabled=frozenset(disabled),
        labels=labels,
    )
