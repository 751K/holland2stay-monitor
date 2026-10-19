"""RENTCafe 申请表的个人资料与加密清单。

从 config.py 拆出：它是自动预订的领域对象，不是全局配置。
"""
from __future__ import annotations

import re
from dataclasses import dataclass


#: 正向白名单的问题不只是漏了几项：它让「新加字段」的默认行为是**明文**。
#: 这张表以后还会跟着 RENTCafe 表单变，每加一个字段就要有人记得回来登记一次。
#: 反过来之后，忘记维护这张表的后果是「多加密了一个不敏感字段」，而不是
#: 「明文存了一项敏感数据」——两种疏忽的代价不对称。
#:
#: 下面三项确实不是个人数据，加密只会让排查变难：
_PLAINTEXT_PROFILE_FIELDS = frozenset({
    "no_middle_name",   # 布尔标记：本人没有中间名
    "min_lease_term",   # 租期偏好，例如 "12 months"
    "housing_type",     # 房型偏好，不指向具体的人
})


def profile_field_is_encrypted(name: str) -> bool:
    """该档案字段是否加密存储。默认加密，见 _PLAINTEXT_PROFILE_FIELDS。"""
    return name not in _PLAINTEXT_PROFILE_FIELDS

#: RENTCafe 申请表的下拉选项，抄自实测页面（Vaals / Katzensprung）。
#: 值必须与页面 option 文本完全一致，否则提交时匹配不上。
APPLICANT_TITLES = ("Mr.", "Ms.", "Mrs.", "Dr.")
APPLICANT_GENDERS = ("Male", "Female", "Gender Nonbinary", "Prefer Not to Disclose")


@dataclass
class ApplicantProfile:
    """RENTCafe 申请表（Applicant Info）里的个人资料。

    证件扫描件**不在这里**，但系统确实要存——见 :mod:`applicant_docs`。
    平台在 `ID/Passport Upload` 到位前拒绝保存申请表的任何内容，而自动预订是
    异步触发的，所以不存在「用完即走的透传」。这是一个知情的取舍（当前部署
    只服务少量熟人，自动预订未对外开放）；文件单独加密落盘，不进这个每轮都要
    加载的配置对象。

    **付款始终不代做。** ``ApplicationCharges`` 那一步要填 IBAN / SWIFT /
    户名（2026-08-03 侦察确认），代填金融凭据是硬限制。

    字段与实测表单一一对应：

    ==================  ================================================
    表单字段             说明
    ==================  ================================================
    Title               见 :data:`APPLICANT_TITLES`
    First/Middle/Last   中间名可用 ``no_middle_name`` 勾选「我没有中间名」
    Phone / Gender      Gender 见 :data:`APPLICANT_GENDERS`
    Date Of Birth       Screening 区块，`YYYY-MM-DD` 存储，提交时转 `d-m-yyyy`
    Nationality         国家名（存显示名而非页面的数字 id——id 会随上游改版
                        失效，显示名更稳，提交时再匹配）
    Country / Address / Post Code-City
    University          Xior 特有的必填项（学生住房）
    min_lease_term      最短租期（月）
    ==================  ================================================
    """

    title: str = ""
    first_name: str = ""
    middle_name: str = ""
    no_middle_name: bool = False
    last_name: str = ""
    phone: str = ""
    gender: str = ""
    date_of_birth: str = ""      # YYYY-MM-DD
    nationality: str = ""
    country: str = "Netherlands"
    address: str = ""
    postcode_city: str = ""
    university: str = ""
    min_lease_term: str = ""
    #: Screening 区块的两个必填项（实测在页面底部的 Information 小节）。
    place_of_birth: str = ""
    #: 护照号 / 身份证号。**加密存储**——它和姓名/生日/地址/国籍凑在一起
    #: 就是一份完整身份信息包，泄露后果与证件扫描件同级。
    id_number: str = ""
    #: 学号，非必填（Xior 是学生住房，填了有助于审核）。
    student_number: str = ""

    # ------------------------------------------------------------------
    # 2026-08-03 对着真实表单补的字段。上一版把 15 个字段名全写错了（命中 0），
    # 修正时才发现表单要的东西比档案里存的多。详见 bookers/rentcafe_form.py。
    # ------------------------------------------------------------------
    #: 地址第二行（门牌补充）。表单把地址拆成 Addr1/Addr2/ZipCode/City 四格，
    #: 原来的 ``postcode_city`` 一格塞两样，对不上。
    address_line2: str = ""
    #: 邮编。留空时从旧的 ``postcode_city`` 里拆（见 :meth:`_split_postcode_city`）。
    postcode: str = ""
    #: 城市。同上。
    city: str = ""
    #: 证件签发国（表单 ``drpDLCountry``，必填）。
    id_country: str = ""
    #: 当前住所性质：``Rent`` / ``Own`` / ``Other``（表单 ``OwnerShipType``）。
    housing_type: str = ""

    # -- 背景调查三问 ---------------------------------------------------
    # 取值 ``Yes`` / ``No``，**留空表示用户还没回答**。
    #
    # 这三项和其它字段有本质区别：它们是关于用户本人的**事实陈述**（是否曾被
    # 驱逐、是否曾被定罪、是否有未决刑事指控）。系统绝不能默认填 "No"——代勾
    # 「我授权你做背景调查」是用户授权过的，代答「我没有前科」不是；答错了是
    # 用户在承担后果。没回答就不提交，让服务端拒绝。
    ever_evicted: str = ""
    ever_convicted: str = ""
    criminal_charges: str = ""

    def _split_postcode_city(self) -> tuple[str, str]:
        """兼容旧档案里 ``postcode_city`` 一格塞两样的写法。

        荷兰邮编形如 ``6291 AB``，后面跟城市名。新字段有值时一律以新字段为准。
        """
        if self.postcode or self.city:
            return self.postcode.strip(), self.city.strip()
        raw = (self.postcode_city or "").strip()
        # 荷兰邮编是 4 位数字 + 2 个字母，城市名跟在后面（可以没有）。
        # 城市部分为空时**不要**把整串当城市——`5652EN` 是邮编不是城市，
        # 贴错标签不会报错，只会往申请表的 City 格里填一串邮编。
        m = re.match(r"^(\d{4}\s*[A-Za-z]{2})\b\s*(.*)$", raw)
        if m:
            return m.group(1).strip(), m.group(2).strip()
        return "", raw

    def is_complete(self) -> bool:
        """是否够填完 Applicant Info 的全部必填项。

        不完整时不该触发半自动预订——填一半的表单提交不上去，只会白白消耗
        RENTCafe 的尝试额度，还在用户账号下留一条废弃申请。
        """
        return not self.missing_fields()

    def missing_fields(self) -> list[str]:
        """还缺哪些必填项，给面板提示用。

        这张清单对着 2026-08-03 的真实表单核过一遍。修正字段名时发现表单要的
        东西比档案里存的多（证件签发国、住所性质、背景调查三问），漏填的后果
        不是报错——服务端会静默丢弃不认识的字段，提交一份缺项的申请。
        """
        postcode, city = self._split_postcode_city()
        checks = {
            "first_name": self.first_name, "last_name": self.last_name,
            "gender": self.gender, "date_of_birth": self.date_of_birth,
            "country": self.country, "address": self.address,
            "postcode": postcode, "city": city,
            "place_of_birth": self.place_of_birth, "id_number": self.id_number,
            "housing_type": self.housing_type,
            # 表单上标着「Nationality」的那一格，字段名其实是 drpDLCountry
            # （见 bookers/rentcafe_applicant._dl_country_attr）
            "nationality": self.nationality,
            # 背景调查三问：留空 = 用户还没回答。系统不替他答，所以这里必须
            # 当成"缺必填项"，而不是当成"答 No"。
            "ever_evicted": self.ever_evicted,
            "ever_convicted": self.ever_convicted,
            "criminal_charges": self.criminal_charges,
        }
        out = [k for k, v in checks.items() if not str(v).strip()]
        # 中间名要么填了，要么显式勾了「没有」——留空且没勾，表单过不了校验
        if not str(self.middle_name).strip() and not self.no_middle_name:
            out.append("middle_name")
        return out

