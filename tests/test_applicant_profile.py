"""申请人档案测试（RENTCafe Applicant Info 自动填）。

半自动预订的设计前提：**系统不碰证件**。流程里那个必填的 ID/Passport 上传
（2026-08-03 实测确实存在）留给用户自己在浏览器里完成——替一群用户保管护照
扫描件是拿一个巨大的泄露责任去换几十秒。

所以这里守两条：
1. 档案里**不能有任何证件字段**；
2. 生日/地址这两项加密存储——它们是身份盗用的关键字段。
"""
from __future__ import annotations

import json

import pytest

from config import (
    APPLICANT_GENDERS,
    APPLICANT_TITLES,
    _ENCRYPTED_PROFILE_FIELDS,
    ApplicantProfile,
    AutoBookConfig,
)
from users import UserConfig, _ab_from_dict, _user_to_row


def _full(**over) -> ApplicantProfile:
    base = dict(
        first_name="J", last_name="Kong", no_middle_name=True, gender="Male",
        date_of_birth="2003-09-14", nationality="China", country="Netherlands",
        address="Dorpsstraat 1", postcode_city="5612 AB Eindhoven",
        university="TU Eindhoven",
        place_of_birth="China", id_number="E12345678",
    )
    base.update(over)
    return ApplicantProfile(**base)


class TestDataBoundary:
    """边界：不存**证件扫描件**，但存**证件号**（用户 2026-08-03 决定）。

    申请表上 `ID number or Passport number *` 是必填，不填就存不了草稿，
    半自动化也就没了意义。所以证件号进档案，并与生日/地址同级加密。
    扫描件仍然不碰——那一步的上传由用户自己在浏览器里完成。
    """

    def test_no_document_upload_fields(self):
        from dataclasses import fields
        names = {f.name for f in fields(ApplicantProfile)}
        for banned in ("id_document", "id_scan", "document", "upload",
                       "attachment", "file", "scan"):
            assert not any(banned in n for n in names), f"档案里不该有 {banned}"

    def test_id_number_is_present_and_encrypted(self):
        from dataclasses import fields
        names = {f.name for f in fields(ApplicantProfile)}
        assert "id_number" in names, "申请表必填，缺了存不了草稿"
        assert "id_number" in _ENCRYPTED_PROFILE_FIELDS, "证件号必须加密存储"


class TestCompleteness:
    def test_full_profile_is_complete(self):
        p = _full()
        assert p.is_complete() is True
        assert p.missing_fields() == []

    def test_empty_profile_is_incomplete(self):
        p = ApplicantProfile()
        assert p.is_complete() is False
        assert "first_name" in p.missing_fields()

    @pytest.mark.parametrize("field", [
        "first_name", "last_name", "gender", "date_of_birth",
        "nationality", "country", "address", "postcode_city", "university",
        "place_of_birth", "id_number",
    ])
    def test_each_required_field_blocks_completion(self, field):
        p = _full(**{field: ""})
        assert p.is_complete() is False
        assert field in p.missing_fields()

    def test_middle_name_needs_value_or_explicit_none(self):
        """表单上「中间名」是必填 + 带一个「我没有中间名」勾选。
        两个都不给，RENTCafe 的前端校验过不去。"""
        assert _full(middle_name="", no_middle_name=False).is_complete() is False
        assert _full(middle_name="Q", no_middle_name=False).is_complete() is True
        assert _full(middle_name="", no_middle_name=True).is_complete() is True

    def test_whitespace_is_not_a_value(self):
        assert _full(university="   ").is_complete() is False

    def test_optional_fields_do_not_block(self):
        """电话、称谓、最短租期留空不影响——表单上它们不带星号。"""
        assert _full(phone="", title="", min_lease_term="").is_complete() is True


class TestPersistence:
    def test_sensitive_fields_are_encrypted(self):
        u = UserConfig(id="u1", name="T",
                       auto_book=AutoBookConfig(applicant_profile=_full()))
        blob = _user_to_row(u)["auto_book_json"]
        assert "2003-09-14" not in blob, "生日不能明文落库"
        assert "Dorpsstraat" not in blob, "地址不能明文落库"

    def test_non_sensitive_fields_stay_plain(self):
        """与库里既有的 email / telegram_chat_id 同级。全都加密会让这张表
        和其它表的处理方式不一致，反而容易在某处漏掉。"""
        u = UserConfig(id="u1", name="T",
                       auto_book=AutoBookConfig(applicant_profile=_full()))
        blob = _user_to_row(u)["auto_book_json"]
        assert "Kong" in blob
        assert "TU Eindhoven" in blob

    def test_round_trip(self):
        u = UserConfig(id="u1", name="T",
                       auto_book=AutoBookConfig(applicant_profile=_full(phone="+31600")))
        back = _ab_from_dict(json.loads(_user_to_row(u)["auto_book_json"])).applicant_profile
        assert back.date_of_birth == "2003-09-14"
        assert back.address == "Dorpsstraat 1"
        assert back.phone == "+31600"
        assert back.is_complete() is True

    def test_encrypted_field_list_matches_reality(self):
        assert set(_ENCRYPTED_PROFILE_FIELDS) == {
            "date_of_birth", "address", "id_number"}

    def test_id_number_is_encrypted(self):
        """护照号/身份证号和姓名生日地址国籍凑在一起就是完整身份信息包。"""
        u = UserConfig(id="u1", name="T",
                       auto_book=AutoBookConfig(applicant_profile=_full()))
        assert "E12345678" not in _user_to_row(u)["auto_book_json"]

    @pytest.mark.parametrize("bad", ["notadict", None, 123, [], {"bogus_key": 1}])
    def test_malformed_input_is_tolerated(self, bad):
        """一个坏字段不该让整个用户配置加载失败——那会连带停掉该用户全部通知。"""
        p = _ab_from_dict({"applicant_profile": bad}).applicant_profile
        assert isinstance(p, ApplicantProfile)

    def test_unknown_keys_are_dropped_not_fatal(self):
        p = _ab_from_dict({"applicant_profile": {
            "first_name": "J", "some_future_field": "x",
        }}).applicant_profile
        assert p.first_name == "J"

    def test_absent_profile_defaults_to_empty(self):
        p = _ab_from_dict({}).applicant_profile
        assert isinstance(p, ApplicantProfile) and not p.is_complete()


class TestFormParsing:
    def _form(self, **kw):
        from werkzeug.datastructures import ImmutableMultiDict
        return ImmutableMultiDict([("AUTO_BOOK_PROFILE_" + k, v) for k, v in kw.items()])

    def test_parses_all_fields(self):
        from app.forms.user_form import parse_applicant_profile
        p = parse_applicant_profile(self._form(
            TITLE="Mr.", FIRST_NAME="J", LAST_NAME="Kong", NO_MIDDLE_NAME="true",
            PHONE="+31600", GENDER="Male", DOB="2003-09-14", NATIONALITY="China",
            COUNTRY="Netherlands", ADDRESS="Dorpsstraat 1",
            POSTCODE_CITY="5612 AB Eindhoven", UNIVERSITY="TU Eindhoven",
            MIN_LEASE_TERM="12", PLACE_OF_BIRTH="China", ID_NUMBER="E12345678",
        ))
        assert p.is_complete() is True
        assert p.no_middle_name is True
        assert p.min_lease_term == "12"

    def test_country_defaults_to_netherlands(self):
        from app.forms.user_form import parse_applicant_profile
        assert parse_applicant_profile(self._form()).country == "Netherlands"

    def test_values_are_stripped(self):
        from app.forms.user_form import parse_applicant_profile
        p = parse_applicant_profile(self._form(FIRST_NAME="  J  "))
        assert p.first_name == "J"

    def test_checkbox_absent_means_false(self):
        from app.forms.user_form import parse_applicant_profile
        assert parse_applicant_profile(self._form()).no_middle_name is False


class TestDropdownsMatchRealForm:
    """选项值必须与实测页面的 option 文本完全一致，否则提交时匹配不上。"""

    def test_titles(self):
        assert APPLICANT_TITLES == ("Mr.", "Ms.", "Mrs.", "Dr.")

    def test_genders(self):
        assert APPLICANT_GENDERS == (
            "Male", "Female", "Gender Nonbinary", "Prefer Not to Disclose")


class TestFormRendering:
    def test_profile_saves_and_renders_back(self, admin_client):
        r = admin_client.post("/users/new", data={
            "name": "prof-ui", "csrf_token": "test_csrf",
            "AUTO_BOOK_PROFILE_FIRST_NAME": "J",
            "AUTO_BOOK_PROFILE_LAST_NAME": "Kong",
            "AUTO_BOOK_PROFILE_NO_MIDDLE_NAME": "true",
            "AUTO_BOOK_PROFILE_GENDER": "Male",
            "AUTO_BOOK_PROFILE_DOB": "2003-09-14",
            "AUTO_BOOK_PROFILE_NATIONALITY": "China",
            "AUTO_BOOK_PROFILE_ADDRESS": "Dorpsstraat 1",
            "AUTO_BOOK_PROFILE_POSTCODE_CITY": "5612 AB Eindhoven",
            "AUTO_BOOK_PROFILE_UNIVERSITY": "TU Eindhoven",
            "AUTO_BOOK_PROFILE_PLACE_OF_BIRTH": "China",
            "AUTO_BOOK_PROFILE_ID_NUMBER": "E12345678",
        }, headers={"X-CSRF-Token": "test_csrf"}, follow_redirects=True)
        assert r.status_code == 200

        from users import load_users
        u = next((x for x in load_users() if x.name == "prof-ui"), None)
        assert u is not None
        assert u.auto_book.applicant_profile.is_complete() is True

        page = admin_client.get(f"/users/{u.id}").get_data(as_text=True)
        # 生日和地址会回填（它们不像密码那样必须隐藏），但库里是加密的
        assert "2003-09-14" in page
        assert "TU Eindhoven" in page


class TestScreeningConsent:
    """代勾法律声明的授权记录。

    申请表上那两句是「我授权做信用/参考/背景调查」和「我确认所填属实」。
    系统替人勾这种声明和替人填地址不是一回事，所以要有显式授权，且授权要
    **能追溯到时刻**——布尔值只能回答「有没有」，回答不了「什么时候」。
    """

    def test_default_is_not_authorised(self):
        assert AutoBookConfig().has_screening_consent() is False

    def test_timestamp_counts_as_authorised(self):
        ab = AutoBookConfig(screening_consent_at="2026-08-03T20:00:00+00:00")
        assert ab.has_screening_consent() is True

    def test_blank_is_not_authorised(self):
        assert AutoBookConfig(screening_consent_at="   ").has_screening_consent() is False

    def test_form_records_a_timestamp_not_a_boolean(self):
        from werkzeug.datastructures import ImmutableMultiDict

        from app.forms.user_form import parse_screening_consent
        got = parse_screening_consent(
            ImmutableMultiDict([("AUTO_BOOK_SCREENING_CONSENT", "true")]))
        assert got and got[:2] == "20", f"应是 ISO 时间戳，得到 {got!r}"

    def test_existing_timestamp_is_not_refreshed(self):
        """每次保存都刷新会把最初的授权时刻抹掉，争议时说不清。"""
        from werkzeug.datastructures import ImmutableMultiDict

        from app.forms.user_form import parse_screening_consent
        old = "2026-01-01T00:00:00+00:00"
        got = parse_screening_consent(
            ImmutableMultiDict([("AUTO_BOOK_SCREENING_CONSENT", "true")]),
            AutoBookConfig(screening_consent_at=old))
        assert got == old

    def test_unchecking_clears_it(self):
        from werkzeug.datastructures import ImmutableMultiDict

        from app.forms.user_form import parse_screening_consent
        got = parse_screening_consent(
            ImmutableMultiDict([]),
            AutoBookConfig(screening_consent_at="2026-01-01T00:00:00+00:00"))
        assert got == ""


class TestNoFieldSilentlyDropped:
    """回归：``_ab_from_dict`` 用显式 kwargs 构造 AutoBookConfig，
    加了新字段却忘了在这里接，就会「存得进、读不出」——而且不报错。

    screening_consent_at 就这么丢过一次：面板上勾了、库里也写了，但每次读回来
    都是空的，表现为「打了勾点保存不生效」。

    这条测试遍历 AutoBookConfig 的全部字段做往返，所以下次再加字段忘了接，
    这里会直接红。
    """

    def test_every_autobook_field_survives_round_trip(self):
        import json
        from dataclasses import fields as dc_fields

        from users import _user_to_row

        probe = {
            "enabled": True, "dry_run": False, "cancel_enabled": True,
            "email": "h@x.com", "password": "pw-h",
            "payment_method": "idealcheckout_visa",
            "xior_email": "x@x.com", "xior_password": "pw-x",
            "ourdomain_email": "o@x.com", "ourdomain_password": "pw-o",
            "screening_consent_at": "2026-08-03T19:00:00+00:00",
            "xior_accounts": {"p1": {"email": "a@x.com", "password": "pw1"}},
        }
        ab = AutoBookConfig(**probe, applicant_profile=_full())
        back = _ab_from_dict(json.loads(
            _user_to_row(UserConfig(id="u", name="t", auto_book=ab))["auto_book_json"]))

        skip = {"listing_filter", "applicant_profile", "xior_accounts"}
        checked = 0
        for f in dc_fields(AutoBookConfig):
            if f.name in skip:
                continue
            assert getattr(back, f.name) == getattr(ab, f.name), (
                f"{f.name} 没能往返——多半是 _ab_from_dict 里漏接了"
            )
            checked += 1
        assert checked >= 10, "探针字段覆盖不足，加了新字段请一并补进 probe"
        # 嵌套结构单独确认
        assert back.applicant_profile.is_complete() is True
        assert back.xior_accounts["p1"]["password"] == "pw1"
