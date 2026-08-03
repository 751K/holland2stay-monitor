"""bookers/rentcafe_applicant.py — 把用户档案映射成 RENTCafe 申请表字段

半自动预订的核心一步：Applicant Info 有 15 个字段，人手填要一两分钟，在抢房
场景里这是决定性的差距。系统自动填完，用户只剩两件系统不该代劳的事——
**上传证件**和**付款**。

字段名全部来自 2026-08-03 对真实页面（Vaals / Katzensprung，已登录状态）的
实测，不是猜的。同一页上还有一批反自动化字段，见 :func:`carry_over_fields`。

只做映射，不发请求：映射规则能脱离网络和账号测试，而它恰恰是最容易因为上游
改版而悄悄错位的部分——错位的后果不是报错，是**默默提交一份填错的申请**。
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime

logger = logging.getLogger(__name__)

#: 档案字段 → 申请表字段名（实测）。
#:
#: 注意 RENTCafe 在不同步骤用了不同的命名风格：注册页是 ``txtName``/``txtName2``，
#: Applicant Info 这一页则另有一套。别把两页的字段名混用。
FIELD_MAP: dict[str, str] = {
    "title": "Title",
    "first_name": "FirstName",
    "middle_name": "MiddleName",
    "last_name": "LastName",
    "phone": "Phone",
    "gender": "Gender",
    "nationality": "Nationality",
    "country": "Country",
    "address": "Address",
    "postcode_city": "PostCodeCity",
    "university": "University",
    "min_lease_term": "MinimumLeaseTerm",
    "place_of_birth": "PlaceOfBirth",
    "id_number": "IDNumber",
    "student_number": "StudentNumber",
}

#: 「我没有中间名」勾选框。中间名在表单上是必填 + 带这个勾选，
#: 两个都不给前端校验就过不去。
NO_MIDDLE_NAME_FIELD = "NoMiddleName"

#: 生日字段单列：它要转格式（见 :func:`to_rentcafe_date`）。
DOB_FIELD = "DateOfBirth"

#: 申请表底部那两个法律声明的勾选框。
#:
#: 一句是「我授权做信用/参考/背景调查」，一句是「我确认所填属实，并同意在
#: 支付申请费后接受审查」。``btnSave`` 的 onclick 会把它标成 required，
#: 所以不勾就存不了草稿。
#:
#: **只有在用户已于面板显式授权（``AutoBookConfig.screening_consent_at``）
#: 时才允许代勾**——代人做法律声明和代人填地址不是一回事。
AGREEMENT_FIELD = "chkAgreement"

#: 入住日字段。值不来自档案，来自选中单元的 ``available_date``。
MOVE_IN_FIELD = "MoveInDate"

#: 页面下发、必须原样回传的反自动化字段。
#:
#: ``txtRenderTime`` 是页面渲染时刻——**提交过快很可能被判为机器人**，
#: 这比 reCAPTCHA 更容易在实现时踩到。``txtvalue2`` 是个空 textarea，
#: 疑似蜜罐，**必须保持为空**。
ANTI_BOT_FIELDS = ("txtCodeVal", "txtRenderTime", "txtvalue1", "txtvalue2")

_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")


class ProfileIncompleteError(Exception):
    """档案缺必填项，不该提交。

    填一半的表单提交不上去，只会白白消耗 RENTCafe 的尝试额度（连续失败会锁
    30 分钟），还在用户账号下留一条废弃申请。
    """


def to_rentcafe_date(value: str) -> str:
    """``YYYY-MM-DD`` → ``d-m-yyyy``（RENTCafe 的显示/提交格式，**不补零**）。

    实测页面上的值形如 ``3-8-2026``，不是 ``03-08-2026``。补零的串没试过，
    与其赌它也能被接受，不如照抄观察到的形状。

    已经是目标格式就原样返回；解析不了则原样返回并记一条日志——这里不该抛，
    调用方的 :func:`build_form_fields` 会在校验阶段统一处理缺失。
    """
    v = (value or "").strip()
    m = _ISO_DATE_RE.match(v)
    if not m:
        if v:
            logger.debug("日期 %r 不是 YYYY-MM-DD，原样透传", v)
        return v
    y, mo, d = m.groups()
    return f"{int(d)}-{int(mo)}-{int(y)}"


def from_rentcafe_date(value: str) -> str:
    """``d-m-yyyy`` → ``YYYY-MM-DD``。解析失败原样返回。"""
    v = (value or "").strip()
    parts = v.split("-")
    if len(parts) != 3:
        return v
    try:
        d, mo, y = (int(p) for p in parts)
        return date(y, mo, d).isoformat()
    except (TypeError, ValueError):
        return v


def carry_over_fields(page_fields: dict) -> dict:
    """从页面下发的隐藏字段里挑出必须原样回传的那些。

    只取反自动化字段——其余隐藏字段由调用方整体带过去。单独抽出来是为了让
    「这几个不能动」这件事在代码里显式可见：``txtvalue2`` 一旦被填上值，
    提交多半会被当成机器人。
    """
    return {k: page_fields.get(k, "") for k in ANTI_BOT_FIELDS if k in page_fields}


def build_form_fields(
    profile,
    *,
    move_in_date: str = "",
    strict: bool = True,
    screening_consent: bool = False,
) -> dict[str, str]:
    """把 :class:`config.ApplicantProfile` 映射成申请表字段。

    Parameters
    ----------
    profile
        用户档案。
    move_in_date
        入住日，取自选中单元的 ``available_date``（``d-m-yyyy``）。留空则不填
        该字段，由页面自带的默认值决定。
    strict
        True（默认）时档案不完整直接抛 :class:`ProfileIncompleteError`。
        设 False 只用于预览/测试——**真实提交路径不要关掉它**。

    Returns
    -------
    ``{表单字段名: 值}``，只含本函数负责的那些；页面下发的隐藏字段和
    反自动化字段由调用方合并（见 :func:`carry_over_fields`）。
    """
    if strict and not profile.is_complete():
        raise ProfileIncompleteError(
            "申请人档案缺必填项，不提交：" + ", ".join(profile.missing_fields())
        )

    out: dict[str, str] = {}
    for attr, field in FIELD_MAP.items():
        val = getattr(profile, attr, "")
        out[field] = str(val).strip() if val is not None else ""

    out[DOB_FIELD] = to_rentcafe_date(getattr(profile, "date_of_birth", ""))

    # 中间名：填了就送值，没填就必须显式勾「我没有」——两者都缺，前端校验不过
    if getattr(profile, "no_middle_name", False):
        out[NO_MIDDLE_NAME_FIELD] = "true"
        out[FIELD_MAP["middle_name"]] = ""
    else:
        out[NO_MIDDLE_NAME_FIELD] = "false"

    if move_in_date:
        out[MOVE_IN_FIELD] = move_in_date.strip()

    # 法律声明只在用户预先授权过时才勾。没授权就留空——服务端会拒，
    # 那正是想要的结果：宁可存不上草稿，也不替人做没授权的法律声明。
    if screening_consent:
        out[AGREEMENT_FIELD] = "on"

    return out
