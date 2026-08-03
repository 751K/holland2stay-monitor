"""bookers/rentcafe_applicant.py — 把用户档案映射成 RENTCafe 申请表字段

字段名**不再硬编码**
--------------------
上一版把 15 个字段名全写错了，命中 0 个——那些名字是从页面可见标签抄的
（「First Name」→ ``FirstName``），真名是 ``ProspectFirstName``。而且有一批
名字里嵌着 prospect id（``drpGender2057105``），本来就不可能写死。

现在改成：字段名由 :mod:`bookers.rentcafe_form` 从**真实页面**解析，这里只
负责「档案的哪一项对应表单的哪一项」。下拉框一律按**选项文字**查提交值
（``Netherlands`` → ``2``），不存 RENTCafe 的内部数字 id。

这类错误不会报错
----------------
填错字段名的后果不是异常，是服务端把不认识的字段**静默丢弃**，提交一份空白
申请。所以这里的每条映射都必须能对着真实页面验证，:func:`build_form_fields`
也会在字段名不存在时明确报出来，而不是默默跳过。
"""
from __future__ import annotations

import logging
import re
from datetime import date

logger = logging.getLogger(__name__)

#: 档案字段 → 表单字段名模板。``{pid}`` 由页面上的 ProspectId 填入。
#:
#: 全部抄自 2026-08-03 真实页面的 ``name`` 属性（Vaals / Katzensprung，
#: 已登录 + 已选中单元）。**改这张表前先去看页面，别照着标签猜。**
TEXT_FIELDS: dict[str, str] = {
    "first_name": "ProspectFirstName",
    "middle_name": "ProspectMiddleName",
    "last_name": "ProspectLastName",
    "phone": "ProspectPhone",
    "address": "Currentaddr{pid}Addr1",
    "address_line2": "Currentaddr{pid}Addr2",
    "postcode": "Currentaddr{pid}ZipCode",
    "city": "Currentaddr{pid}City",
    # Xior 自定义的三项。字段名里的 ADDITIOAL 是**上游的拼写错误**，
    # 照抄，别"顺手修好"。
    "place_of_birth": "STU_BIRTHPLACEGUESTCARD_ADDITIOALINFO",
    "id_number": "STU_IDGUESTCARD_ADDITIOALINFO",
    "student_number": "STU_NUMBERGUESTCARD_ADDITIOALINFO",
}

#: 下拉框：档案字段 → 字段名模板。值按**选项文字**查（见 ``option_value``）。
SELECT_FIELDS: dict[str, str] = {
    "title": "Salutation",
    "gender": "drpGender{pid}",
    "country": "drpCurrentCountry",
    "id_country": "drpDLCountry",
    "housing_type": "OwnerShipType{pid}",
}

#: 背景调查三问。**系统绝不替用户默认答**——见 :func:`build_form_fields`。
SCREENING_QUESTIONS: dict[str, str] = {
    "ever_evicted": "drpEverEvicted",
    "ever_convicted": "drpEverConvicted",
    "criminal_charges": "drpCriminalCharges",
}

#: 生日：存 ``YYYY-MM-DD``，提交转 ``d-m-yyyy``。
DOB_FIELD = "ProspectDOB{pid}"

#: 「我没有中间名」勾选框（实测 value='1'）。中间名是必填，留空又不勾，
#: 前端校验过不去。
NO_MIDDLE_NAME_FIELD = "chkMiddleName"

#: 学生类别。取值就是单元 ``ContinueClick`` 里的 SchoolId（实测 648=Dutch），
#: 所以不用用户填——从选中的单元推出来即可。
SCHOOL_FIELD = "drpSchool"

#: 申请表底部那两个法律声明的勾选框（实测两个，不是一个）。
#:
#: 一句授权做信用/背景调查，一句确认所填属实。**只有用户在面板显式授权过
#: （``AutoBookConfig.screening_consent_at``）才允许代勾**——代人做法律声明
#: 和代人填地址不是一回事。
AGREEMENT_FIELDS = ("chkTandCAgree", "chkTandCAgreeLegal")

#: 页面下发、必须原样回传的反自动化字段。
#:
#: ``txtRenderTime`` 是页面渲染时刻——**提交过快很可能被判为机器人**。
#: ``txtvalue2`` 是个空 textarea，疑似蜜罐，**必须保持为空**。
ANTI_BOT_FIELDS = ("txtCodeVal", "txtRenderTime", "txtvalue1", "txtvalue2")

_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")


class ProfileIncompleteError(Exception):
    """档案缺必填项，不该提交。

    填一半的表单提交不上去，只会白白消耗 RENTCafe 的尝试额度（连续失败会锁
    30 分钟），还在用户账号下留一条废弃申请。
    """


class FormShapeChangedError(Exception):
    """页面上找不到本该存在的字段——上游改版了。

    必须显式失败。**默默跳过等于提交一份缺项的申请**，而服务端对不认识的
    字段是静默丢弃的，用户看不出任何异常。
    """


def to_rentcafe_date(value: str) -> str:
    """``YYYY-MM-DD`` → ``d-m-yyyy``（RENTCafe 的格式，**不补零**）。

    实测页面上的值形如 ``3-8-2026``，不是 ``03-08-2026``。
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
    「这几个不能动」这件事在代码里显式可见。
    """
    return {k: page_fields.get(k, "") for k in ANTI_BOT_FIELDS if k in page_fields}


def _addr2_attr(form, name: str) -> str:
    """地址第二行那一格实际在要什么——**看标签，别看字段名**。

    Xior 把 ``Currentaddr{pid}Addr2`` 的标签改成了「University」并设为必填
    （2026-08-03 实测，Vaals）。字段名还是地址第二行，要的却是学校名。只按
    字段名映射的话，用户填的大学名无处可去，而这一格被填进一段地址——两边
    都错，且不会报任何错。
    """
    label = form.label_of(name).casefold()
    if "universit" in label or "school" in label or "大学" in label:
        return "university"
    return "address_line2"


def _dl_country_attr(form, name: str) -> str:
    """``drpDLCountry`` 那一格实际在要什么——同样看标签。

    字段名是 RENTCafe 的「驾照签发国」，Xior 把标签改成了「Nationality」
    （2026-08-03 实测）。所以这里该喂档案里的 ``nationality``，而不是按字段名
    去理解成证件签发国。
    """
    label = form.label_of(name).casefold()
    if "national" in label or "国籍" in label:
        return "nationality"
    return "id_country"


def _profile_value(profile, attr: str) -> str:
    """取档案里的值。

    ``postcode`` / ``city`` 走一层兼容：表单把地址拆成四格，而旧档案是
    ``postcode_city`` 一格塞两样（「6291 AB Vaals」）。老用户不该因为字段
    改名就被判档案不完整。
    """
    if attr in ("postcode", "city"):
        split = getattr(profile, "_split_postcode_city", None)
        if callable(split):
            postcode, city = split()
            return postcode if attr == "postcode" else city
    val = getattr(profile, attr, "") or ""
    return str(val).strip()


def _put(out: dict, form, name: str, value: str, *, required: bool) -> None:
    """写一个字段；字段在页面上不存在或被 disabled 时按 required 决定死活。

    ``disabled`` 的字段一律不发：jQuery 不序列化它们，那是服务端自己算的值
    （实测 ``LeaseTerm`` / ``ProspectEmail`` / ``PrefMoveinDate{pid}`` 都是）。
    """
    if name in form.disabled:
        logger.debug("字段 %s 是 disabled，交给服务端自己填", name)
        return
    if name not in form.names:
        if required:
            raise FormShapeChangedError(
                f"申请表上找不到字段 {name!r}——页面结构可能已变，已中止提交。"
            )
        logger.debug("字段 %s 不在本页上，跳过", name)
        return
    out[name] = value


def build_form_fields(
    profile,
    form,
    *,
    school_id: str = "",
    strict: bool = True,
    screening_consent: bool = False,
) -> dict[str, str]:
    """把 :class:`config.ApplicantProfile` 映射成申请表字段。

    Parameters
    ----------
    profile
        用户档案。
    form
        :class:`bookers.rentcafe_form.ApplicantForm`，从**当前页面**解析得来。
        字段名和下拉取值都要现查——名字里带 prospect id，硬编码不了。
    school_id
        学生类别，取自选中单元 ``ContinueClick`` 的 SchoolId 参数。
    strict
        True（默认）时档案不完整直接抛 :class:`ProfileIncompleteError`。
        设 False 只用于预览——**真实提交路径不要关掉它**。
    screening_consent
        用户是否已在面板显式授权代勾法律声明。

    Returns
    -------
    ``{表单字段名: 值}``。页面下发的隐藏字段和反自动化字段由调用方合并。
    """
    if strict and not profile.is_complete():
        raise ProfileIncompleteError(
            "申请人档案缺必填项，不提交：" + ", ".join(profile.missing_fields())
        )

    out: dict[str, str] = {}

    for attr, template in TEXT_FIELDS.items():
        name = form.pid(template)
        if attr == "address_line2":
            attr = _addr2_attr(form, name)
        val = _profile_value(profile, attr)
        _put(out, form, name, val, required=attr in _REQUIRED_TEXT)

    for attr, template in SELECT_FIELDS.items():
        name = form.pid(template)
        if attr == "id_country":
            attr = _dl_country_attr(form, name)
        label = str(getattr(profile, attr, "") or "").strip()
        if not label:
            continue
        value = form.option_value(name, label)
        if not value:
            # 选项文字对不上是真问题：静默留空会提交一份缺项的申请。
            raise FormShapeChangedError(
                f"下拉框 {name!r} 里没有选项 {label!r}——"
                f"档案里的值与平台选项对不上，已中止提交。"
            )
        _put(out, form, name, value, required=True)

    _put(out, form, form.pid(DOB_FIELD),
         to_rentcafe_date(getattr(profile, "date_of_birth", "")), required=True)

    # 中间名：填了就送值，没填就必须显式勾「我没有」——两者都缺，校验不过。
    if getattr(profile, "no_middle_name", False):
        _put(out, form, NO_MIDDLE_NAME_FIELD, "1", required=False)
        out.pop(form.pid(TEXT_FIELDS["middle_name"]), None)

    if school_id:
        _put(out, form, SCHOOL_FIELD, str(school_id).strip(), required=False)

    # 背景调查三问是关于用户本人的**事实陈述**（是否曾被驱逐 / 定罪 / 有未决
    # 刑事指控）。代勾「我授权你做背景调查」和代答「我没有前科」完全是两回事：
    # 前者用户授权过，后者只有用户自己知道答案。**没答就不填**，让服务端拒绝
    # ——宁可存不上，也不替人做一份可能不实的法律陈述。
    for attr, name in SCREENING_QUESTIONS.items():
        answer = str(getattr(profile, attr, "") or "").strip()
        if answer:
            _put(out, form, name, answer, required=False)

    # 法律声明同理：只在用户预先授权过时才勾。
    if screening_consent:
        for name in AGREEMENT_FIELDS:
            _put(out, form, name, "1", required=False)

    return out


#: 哪些文本字段缺了就该中止。其余（中间名、地址第二行、学号）可以为空。
_REQUIRED_TEXT = frozenset({
    "first_name", "last_name", "address", "postcode", "city",
    "place_of_birth", "id_number",
    # Xior 把地址第二行的标签改成「University」并设为必填（见 _addr2_attr）
    "university",
})
