"""申请表字段映射测试。

映射错位的后果**不是报错**：服务端对不认识的字段是静默丢弃的，提交上去的是
一份空白申请，用户看不出任何异常。所以这一层必须靠测试钉死。

2026-08-03 血的教训：上一版 15 个字段名**全是错的，命中 0**——名字是从页面
可见标签抄的（「First Name」→ ``FirstName``），真名是 ``ProspectFirstName``。
而旧测试全绿，因为它是这么写的::

    got = build_form_fields(profile)
    assert got[FIELD_MAP["first_name"]] == "J"     # 拿映射表查映射表的输出

**它在验证自己**，跟页面上真实叫什么毫无关系。所以现在的 fixture 一律按真实
页面的 ``name`` 属性写死，包括几个反直觉的地方：

- 一批字段名里嵌着 prospect id（``drpGender2057105``）——硬编码不了；
- 下拉提交的是 RENTCafe 内部数字 id（Netherlands→``2``），不是显示名；
- Xior 自定义字段里 ``ADDITIOAL`` 是上游的拼写错误，照抄。
"""
from __future__ import annotations

import pytest

from bookers.rentcafe_applicant import (
    AGREEMENT_FIELDS,
    ANTI_BOT_FIELDS,
    NO_MIDDLE_NAME_FIELD,
    FormShapeChangedError,
    ProfileIncompleteError,
    build_form_fields,
    carry_over_fields,
    from_rentcafe_date,
    to_rentcafe_date,
)
from bookers.rentcafe_form import parse_applicant_form
from config import ApplicantProfile

PID = "2057105"

#: 按真实页面结构写的紧凑 fixture（字段名、选项值都是实测抄录）。
PAGE = f"""
<form id="ApplicantInformation" name="ApplicantInformation">
  <input type="hidden" name="ProspectId" value="{PID}">
  <select name="Salutation"><option value=""></option>
    <option value="Mr.">Mr.</option><option value="Ms.">Ms.</option></select>
  <input type="text" name="ProspectFirstName" value="">
  <input type="text" name="ProspectMiddleName" value="">
  <input type="checkbox" name="chkMiddleName" value="1">
  <input type="text" name="ProspectLastName" value="">
  <input type="text" name="ProspectPhone" value="">
  <input type="text" name="ProspectEmail" value="a@b.c" disabled="True">
  <input type="text" name="PrefMoveinDate{PID}" value="16-8-2026" disabled="True">
  <input type="text" name="LeaseTerm" value="" disabled="True">
  <select name="drpGender{PID}"><option value=""></option>
    <option value="M">Male</option><option value="F">Female</option></select>
  <select name="drpSchool"><option value="648">Dutch</option>
    <option value="664">International - Standard</option></select>
  <select name="drpCurrentCountry"><option value="2">Netherlands</option>
    <option value="49">China</option></select>
  <input type="text" name="Currentaddr{PID}Addr1" value="">
  <label for="CurrentAddress2{PID}">University <span>*</span></label>
  <input type="text" name="Currentaddr{PID}Addr2" value="">
  <input type="text" name="Currentaddr{PID}ZipCode" value="">
  <input type="text" name="Currentaddr{PID}City" value="">
  <select name="OwnerShipType{PID}"><option value=""></option>
    <option value="Rent">Rent</option><option value="Own">Own</option></select>
  <input type="text" name="ProspectDOB{PID}" value="">
  <label for="DLCountry">Nationality <span>*</span></label>
  <select name="drpDLCountry"><option value=""></option>
    <option value="2">Netherlands</option><option value="49">China</option></select>
  <select name="drpEverEvicted"><option value=""></option>
    <option value="Yes">Yes</option><option value="No">No</option></select>
  <select name="drpEverConvicted"><option value=""></option>
    <option value="Yes">Yes</option><option value="No">No</option></select>
  <select name="drpCriminalCharges"><option value=""></option>
    <option value="Yes">Yes</option><option value="No">No</option></select>
  <input type="checkbox" name="chkTandCAgree" value="1">
  <input type="checkbox" name="chkTandCAgreeLegal" value="1">
  <input type="text" name="STU_BIRTHPLACEGUESTCARD_ADDITIOALINFO" value="">
  <input type="text" name="STU_IDGUESTCARD_ADDITIOALINFO" value="">
  <input type="text" name="STU_NUMBERGUESTCARD_ADDITIOALINFO" value="">
</form>
"""


@pytest.fixture
def form():
    f = parse_applicant_form(PAGE)
    assert f is not None and f.prospect_id == PID
    return f


def _full(**over) -> ApplicantProfile:
    base = dict(
        title="Mr.", first_name="J", last_name="Kong", no_middle_name=True,
        phone="+31600", gender="Male", date_of_birth="2003-09-14",
        country="Netherlands", address="Dorpsstraat 1",
        postcode="5612 AB", city="Eindhoven",
        place_of_birth="China", id_number="E12345678",
        nationality="China", university="Fontys", housing_type="Rent",
        ever_evicted="No", ever_convicted="No", criminal_charges="No",
    )
    base.update(over)
    return ApplicantProfile(**base)


class TestDateFormat:
    @pytest.mark.parametrize("iso,want", [
        ("2003-09-14", "14-9-2003"), ("2026-08-03", "3-8-2026"),
    ])
    def test_iso_to_rentcafe_does_not_zero_pad(self, iso, want):
        assert to_rentcafe_date(iso) == want

    @pytest.mark.parametrize("bad", ["", "not-a-date", "14-9-2003"])
    def test_unparseable_passes_through(self, bad):
        assert to_rentcafe_date(bad) == bad

    def test_round_trip(self):
        assert from_rentcafe_date(to_rentcafe_date("2003-09-14")) == "2003-09-14"


class TestRealFieldNames:
    """每条断言的右边都必须是**页面上真实存在的 name**。"""

    def test_uses_prospect_prefixed_names(self, form):
        out = build_form_fields(_full(), form)
        assert out["ProspectFirstName"] == "J"
        assert out["ProspectLastName"] == "Kong"
        assert "FirstName" not in out, "FirstName 是可见标签，不是字段名"
        assert "LastName" not in out

    def test_uses_prospect_id_suffixed_names(self, form):
        out = build_form_fields(_full(), form)
        assert out[f"drpGender{PID}"] == "M"
        assert out[f"ProspectDOB{PID}"] == "14-9-2003"
        assert out[f"Currentaddr{PID}Addr1"] == "Dorpsstraat 1"
        assert out[f"Currentaddr{PID}ZipCode"] == "5612 AB"
        assert out[f"Currentaddr{PID}City"] == "Eindhoven"

    def test_uses_xior_custom_field_names_typo_and_all(self, form):
        """``ADDITIOAL`` 是上游的拼写错误——照抄，别顺手修好。"""
        out = build_form_fields(_full(), form)
        assert out["STU_BIRTHPLACEGUESTCARD_ADDITIOALINFO"] == "China"
        assert out["STU_IDGUESTCARD_ADDITIOALINFO"] == "E12345678"


class TestSelectsSubmitInternalIds:
    """下拉提交的是 RENTCafe 的内部 id，不是显示名。"""

    def test_country_becomes_a_numeric_id(self, form):
        assert build_form_fields(_full(country="Netherlands"), form)["drpCurrentCountry"] == "2"

    def test_gender_becomes_a_code(self, form):
        assert build_form_fields(_full(gender="Female"), form)[f"drpGender{PID}"] == "F"

    def test_unknown_option_aborts_instead_of_submitting_blank(self, form):
        """选项对不上必须中止。静默留空 = 提交一份缺项的申请。"""
        with pytest.raises(FormShapeChangedError, match="Freedonia"):
            build_form_fields(_full(country="Freedonia"), form)


class TestLabelsDecideWhatAFieldWants:
    """物业会**改标签复用字段**——光看字段名会把值填错格，且不报任何错。

    2026-08-03 实测（Vaals）：

        Currentaddr{pid}Addr2  字段名是「地址第二行」，标签写着 University
        drpDLCountry           字段名是「驾照签发国」，标签写着 Nationality

    只按字段名映射的话，用户填的大学名无处可去，而地址第二行被填进一段地址。
    """

    def test_address_line2_takes_the_university_when_labelled_so(self, form):
        out = build_form_fields(_full(university="Fontys"), form)
        assert out[f"Currentaddr{PID}Addr2"] == "Fontys"

    def test_dl_country_takes_the_nationality_when_labelled_so(self, form):
        out = build_form_fields(_full(nationality="China"), form)
        assert out["drpDLCountry"] == "49"

    def test_falls_back_to_the_field_name_meaning_without_a_label(self):
        """没有那两个标签时，按字段名本来的含义走。"""
        plain = PAGE.replace("University <span>*</span>", "Address line 2")
        plain = plain.replace("Nationality <span>*</span>", "ID issuing country")
        f = parse_applicant_form(plain)
        out = build_form_fields(
            _full(address_line2="Apt 3", id_country="Netherlands"), f, strict=False,
        )
        assert out[f"Currentaddr{PID}Addr2"] == "Apt 3"
        assert out["drpDLCountry"] == "2"


class TestDisabledFieldsAreNotSent:
    """jQuery 不序列化 disabled 字段，那是服务端自己算的值。"""

    @pytest.mark.parametrize("name", ["LeaseTerm", "ProspectEmail"])
    def test_disabled_scalar_fields_omitted(self, form, name):
        assert name not in build_form_fields(_full(), form)

    def test_move_in_date_is_the_servers_job(self, form):
        assert f"PrefMoveinDate{PID}" not in build_form_fields(_full(), form)


class TestScreeningQuestionsAreNeverAnsweredForTheUser:
    """背景调查三问是关于用户本人的**事实陈述**，系统不能替他答。

    代勾「我授权你做背景调查」是用户授权过的；代答「我没有前科」不是——
    答错了是用户在承担后果。
    """

    def test_answers_are_passed_through_when_given(self, form):
        out = build_form_fields(_full(ever_evicted="No", ever_convicted="Yes"), form)
        assert out["drpEverEvicted"] == "No"
        assert out["drpEverConvicted"] == "Yes"

    def test_unanswered_are_not_defaulted_to_no(self, form):
        out = build_form_fields(
            _full(ever_evicted="", ever_convicted="", criminal_charges=""),
            form, strict=False,
        )
        for f in ("drpEverEvicted", "drpEverConvicted", "drpCriminalCharges"):
            assert f not in out, "没回答就不能替用户填，哪怕填 No 看起来更顺"

    def test_unanswered_blocks_submission_in_strict_mode(self):
        p = _full(ever_evicted="")
        assert not p.is_complete()
        assert "ever_evicted" in p.missing_fields()


class TestAgreementNeedsConsent:
    def test_both_checkboxes_ticked_with_consent(self, form):
        out = build_form_fields(_full(), form, screening_consent=True)
        for f in AGREEMENT_FIELDS:
            assert out[f] == "1"

    def test_nothing_ticked_without_consent(self, form):
        out = build_form_fields(_full(), form, screening_consent=False)
        for f in AGREEMENT_FIELDS:
            assert f not in out

    def test_there_are_two_of_them(self):
        """实测页面底部是两个勾选框，不是一个——只勾一个照样过不了。"""
        assert set(AGREEMENT_FIELDS) == {"chkTandCAgree", "chkTandCAgreeLegal"}


class TestSchoolComesFromTheUnit:
    def test_school_id_is_passed_through(self, form):
        assert build_form_fields(_full(), form, school_id="648")["drpSchool"] == "648"

    def test_absent_school_id_leaves_it_alone(self, form):
        assert "drpSchool" not in build_form_fields(_full(), form)


class TestMiddleName:
    def test_no_middle_name_ticks_the_box_and_sends_no_value(self, form):
        out = build_form_fields(_full(no_middle_name=True), form)
        assert out[NO_MIDDLE_NAME_FIELD] == "1"
        assert "ProspectMiddleName" not in out

    def test_middle_name_present_is_sent(self, form):
        out = build_form_fields(_full(middle_name="Q", no_middle_name=False), form)
        assert out["ProspectMiddleName"] == "Q"
        assert NO_MIDDLE_NAME_FIELD not in out


class TestLegacyPostcodeCity:
    """老档案把邮编和城市塞在一格里，不该因为字段拆开就判为不完整。"""

    def test_split_from_the_old_single_field(self, form):
        p = _full(postcode="", city="", postcode_city="6291 AB Vaals")
        out = build_form_fields(p, form)
        assert out[f"Currentaddr{PID}ZipCode"] == "6291 AB"
        assert out[f"Currentaddr{PID}City"] == "Vaals"

    def test_new_fields_win_when_both_present(self, form):
        p = _full(postcode="1111 AA", city="Delft", postcode_city="6291 AB Vaals")
        assert build_form_fields(p, form)[f"Currentaddr{PID}City"] == "Delft"

    @pytest.mark.parametrize("raw,postcode,city", [
        ("6291 AB Vaals", "6291 AB", "Vaals"),
        ("5652EN Eindhoven", "5652EN", "Eindhoven"),
        # 只填了邮编、没填城市——**不能把邮编当城市**。实测 demo 档案里就是
        # 这样，旧写法会往申请表的 City 格里填一串邮编，且毫无报错。
        ("5652EN", "5652EN", ""),
        # 反过来，只填了城市名
        ("Eindhoven", "", "Eindhoven"),
    ])
    def test_postcode_is_never_mistaken_for_a_city(self, raw, postcode, city):
        p = _full(postcode="", city="", postcode_city=raw)
        assert p._split_postcode_city() == (postcode, city)


class TestCompletenessGate:
    def test_incomplete_profile_raises(self, form):
        with pytest.raises(ProfileIncompleteError):
            build_form_fields(_full(first_name=""), form)

    def test_strict_false_is_for_preview_only(self, form):
        build_form_fields(_full(first_name=""), form, strict=False)

    def test_new_required_fields_are_gated(self):
        for attr in ("nationality", "housing_type", "place_of_birth"):
            assert attr in _full(**{attr: ""}).missing_fields()


class TestFormShapeChanges:
    def test_missing_required_field_aborts(self):
        """页面上少了必填字段 = 上游改版，必须显式失败。"""
        f = parse_applicant_form(
            PAGE.replace('name="ProspectFirstName"', 'name="Renamed"')
        )
        with pytest.raises(FormShapeChangedError, match="ProspectFirstName"):
            build_form_fields(_full(), f)

    def test_page_without_prospect_id_is_unusable(self):
        """没有 ProspectId 就定位不了那批带后缀的字段名。"""
        assert parse_applicant_form(PAGE.replace('name="ProspectId"', 'name="X"')) is None

    def test_non_applicant_page_returns_none(self):
        assert parse_applicant_form("<html>login</html>") is None


class TestAntiBotFields:
    def test_carries_over_only_known_fields(self):
        got = carry_over_fields({"txtCodeVal": "c", "txtRenderTime": "t", "Other": "x"})
        assert got == {"txtCodeVal": "c", "txtRenderTime": "t"}

    def test_honeypot_stays_empty(self):
        assert carry_over_fields({"txtvalue2": ""})["txtvalue2"] == ""

    def test_absent_fields_are_not_invented(self):
        assert carry_over_fields({}) == {}

    def test_render_time_is_carried_not_regenerated(self):
        """提交过快会被判为机器人——渲染时刻必须原样回传。"""
        assert "txtRenderTime" in ANTI_BOT_FIELDS
        assert carry_over_fields({"txtRenderTime": "42"})["txtRenderTime"] == "42"
