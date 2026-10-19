"""申请人档案测试（RENTCafe Applicant Info 自动填）。

半自动预订的设计前提：**系统不碰证件**。流程里那个必填的 ID/Passport 上传
（2026-08-03 实测确实存在）留给用户自己在浏览器里完成——替一群用户保管护照
扫描件是拿一个巨大的泄露责任去换几十秒。

所以这里守两条：
1. 档案里**不能有任何证件字段**；
2. 生日/地址这两项加密存储——它们是身份盗用的关键字段。
"""
from __future__ import annotations

import dataclasses
import json

import pytest

from config import (
    APPLICANT_GENDERS,
    APPLICANT_TITLES,
    _PLAINTEXT_PROFILE_FIELDS,
    profile_field_is_encrypted,
    ApplicantProfile,
    AutoBookConfig,
)
from crypto import decrypt, encrypt
from users import UserConfig, _ab_from_dict, _user_to_row


def _full(**over) -> ApplicantProfile:
    base = dict(
        first_name="J", last_name="Kong", no_middle_name=True, gender="Male",
        date_of_birth="2003-09-14", nationality="China", country="Netherlands",
        address="Dorpsstraat 1", postcode_city="5612 AB Eindhoven",
        university="TU Eindhoven",
        place_of_birth="China", id_number="E12345678",
        id_country="China", housing_type="Rent",
        ever_evicted="No", ever_convicted="No", criminal_charges="No",
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
        assert profile_field_is_encrypted("id_number"), "证件号必须加密存储"


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
        "country", "address",
        "place_of_birth", "id_number", "nationality", "housing_type",
        # 背景调查三问：留空表示用户还没回答，不能当成答 "No"
        "ever_evicted", "ever_convicted", "criminal_charges",
    ])
    def test_each_required_field_blocks_completion(self, field):
        p = _full(**{field: ""})
        assert p.is_complete() is False
        assert field in p.missing_fields()

    def test_address_needs_a_postcode_and_city_from_somewhere(self):
        """邮编/城市可以来自新字段，也可以来自旧的 postcode_city 一格。

        表单把地址拆成 Addr1/Addr2/ZipCode/City 四格，而老档案是一格塞两样。
        老用户不该因为字段拆开就突然被判档案不完整——但两边都空就是真的缺。
        """
        legacy = _full(postcode="", city="", postcode_city="5612 AB Eindhoven")
        assert legacy.is_complete() is True

        blank = _full(postcode="", city="", postcode_city="")
        assert blank.is_complete() is False
        assert "postcode" in blank.missing_fields()
        assert "city" in blank.missing_fields()

    def test_middle_name_needs_value_or_explicit_none(self):
        """表单上「中间名」是必填 + 带一个「我没有中间名」勾选。
        两个都不给，RENTCafe 的前端校验过不去。"""
        assert _full(middle_name="", no_middle_name=False).is_complete() is False
        assert _full(middle_name="Q", no_middle_name=False).is_complete() is True
        assert _full(middle_name="", no_middle_name=True).is_complete() is True

    def test_whitespace_is_not_a_value(self):
        assert _full(place_of_birth="   ").is_complete() is False

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

    def test_names_and_school_are_encrypted_too(self):
        """2026-08-24 政策反转：姓名和学校也加密了。

        原先的理由是「与库里既有的 email / telegram_chat_id 同级，保持明文」。
        但这张档案不是零散字段——它是一整包身份信息（姓名 + 生日 + 地址 +
        国籍 + 证件号 + 学校），凑在一起的敏感度远高于任何单项。
        """
        u = UserConfig(id="u1", name="T",
                       auto_book=AutoBookConfig(applicant_profile=_full()))
        blob = _user_to_row(u)["auto_book_json"]
        assert "Kong" not in blob, "姓氏明文落库了"
        assert "TU Eindhoven" not in blob, "学校明文落库了"

    def test_preference_fields_stay_plain(self):
        """例外清单里的三项确实不是个人数据，加密只会让排查变难。"""
        from config import _PLAINTEXT_PROFILE_FIELDS
        for f in _PLAINTEXT_PROFILE_FIELDS:
            assert not profile_field_is_encrypted(f)

    def test_round_trip(self):
        u = UserConfig(id="u1", name="T",
                       auto_book=AutoBookConfig(applicant_profile=_full(phone="+31600")))
        back = _ab_from_dict(json.loads(_user_to_row(u)["auto_book_json"])).applicant_profile
        assert back.date_of_birth == "2003-09-14"
        assert back.address == "Dorpsstraat 1"
        assert back.phone == "+31600"
        assert back.is_complete() is True

    def test_encryption_is_the_default_not_a_whitelist(self):
        """2026-08-24 从「只加密三项」改成「默认加密 + 例外清单」。

        原先的正向白名单漏掉了国籍、出生地、性别、电话，以及
        ever_evicted / ever_convicted / criminal_charges —— 犯罪记录在 GDPR 下
        属第 10 条数据，比证件号更该保护，却一直是明文。详见
        tests/test_applicant_profile_encryption.py。
        """
        from config import _PLAINTEXT_PROFILE_FIELDS
        assert profile_field_is_encrypted("criminal_charges")
        assert profile_field_is_encrypted("nationality")
        assert _PLAINTEXT_PROFILE_FIELDS == {
            "no_middle_name", "min_lease_term", "housing_type"}

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
            POSTCODE="5612 AB", CITY="Eindhoven",
            ID_COUNTRY="China", HOUSING_TYPE="Rent",
            EVER_EVICTED="No", EVER_CONVICTED="No", CRIMINAL_CHARGES="No",
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
            "AUTO_BOOK_PROFILE_POSTCODE": "5612 AB",
            "AUTO_BOOK_PROFILE_CITY": "Eindhoven",
            "AUTO_BOOK_PROFILE_ID_COUNTRY": "China",
            "AUTO_BOOK_PROFILE_HOUSING_TYPE": "Rent",
            "AUTO_BOOK_PROFILE_EVER_EVICTED": "No",
            "AUTO_BOOK_PROFILE_EVER_CONVICTED": "No",
            "AUTO_BOOK_PROFILE_CRIMINAL_CHARGES": "No",
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


# ── 字段加密、往返与迁移（原 test_applicant_profile_encryption.py） ──
#
# 这张档案是给 RENTCafe 租房申请表填的，服务端存着：
#
#     id_number       身份证 / 护照号
#     date_of_birth   出生日期
#     nationality / place_of_birth / gender / phone / student_number
#     ever_evicted / ever_convicted / criminal_charges   ← GDPR 第 10 条数据
#
# 2026-08-24 之前只有 date_of_birth / address / id_number 三项加密，**犯罪记录和
# 国籍是明文**。改成「默认加密 + 例外清单」之后，忘记维护清单的后果从「明文存了
# 一项敏感数据」变成「多加密了一个不敏感字段」——两种疏忽的代价不对称。

_ALL = [f.name for f in dataclasses.fields(ApplicantProfile)]


class TestWhichFieldsAreEncrypted:
    def test_default_is_encrypted(self):
        """新加字段默认加密——这是这次改动的全部要点。"""
        assert profile_field_is_encrypted("some_field_added_next_year")

    @pytest.mark.parametrize("field", [
        "id_number", "date_of_birth", "address", "address_line2",
        "nationality", "place_of_birth", "gender", "phone",
        "student_number", "postcode", "postcode_city", "city",
        "first_name", "middle_name", "last_name", "university",
    ])
    def test_personal_data_is_encrypted(self, field):
        assert field in _ALL, f"{field} 不再是档案字段了，用例要跟着改"
        assert profile_field_is_encrypted(field)

    @pytest.mark.parametrize("field", ["ever_evicted", "ever_convicted", "criminal_charges"])
    def test_criminal_history_is_encrypted(self, field):
        """GDPR 第 10 条数据。改动前这三项是明文，比身份证号还敏感。"""
        assert profile_field_is_encrypted(field)

    def test_plaintext_list_is_small_and_justified(self):
        """例外清单每多一项就多一分风险，别让它悄悄变长。"""
        assert _PLAINTEXT_PROFILE_FIELDS == {
            "no_middle_name", "min_lease_term", "housing_type",
        }

    def test_every_plaintext_field_actually_exists(self):
        for f in _PLAINTEXT_PROFILE_FIELDS:
            assert f in _ALL, f"例外清单里的 {f} 已经不是档案字段，属于陈旧配置"


class TestEncryptedRoundTrip:
    def test_saved_profile_is_encrypted_at_rest(self):
        from users import UserConfig, _user_to_row
        # 明文取足够长且可区分的值：短字符串（"No"）会**碰巧出现在别的字段的
        # base64 密文里**，让"明文没泄漏"这条断言假失败。第一版就踩了这个。
        prof = {"id_number": "IDNUM-QQQQ-7788", "criminal_charges": "CRIMREC-ZZZZ",
                "nationality": "NATION-WWWW", "no_middle_name": True}
        u = UserConfig(id="u1", name="t")
        u.auto_book.applicant_profile = ApplicantProfile(**{
            k: v for k, v in prof.items()
        })
        row = _user_to_row(u)
        stored = json.loads(row["auto_book_json"])["applicant_profile"]
        for k in ("id_number", "criminal_charges", "nationality"):
            assert stored[k].startswith("$F$"), f"{k} 落库时没加密"
            assert prof[k] not in row["auto_book_json"], f"{k} 的明文出现在了库里"
        assert stored["no_middle_name"] is True, "布尔的例外字段不该被加密"

    def test_round_trip_returns_the_original(self):
        from users import _profile_from_dict
        raw = {"id_number": encrypt("AB1234567"),
               "criminal_charges": encrypt("No"),
               "no_middle_name": True}
        got = _profile_from_dict(raw)
        assert got.id_number == "AB1234567"
        assert got.criminal_charges == "No"
        assert got.no_middle_name is True

    def test_legacy_plaintext_still_reads(self):
        """decrypt 对无前缀的值向后兼容，老数据不能因为改了清单就读不出来。"""
        from users import _profile_from_dict
        got = _profile_from_dict({"nationality": "Chinese"})
        assert got.nationality == "Chinese"


class TestEncryptIsIdempotent:
    """迁移和保存路径都可能对同一个值调两次。"""

    def test_double_encrypt_is_a_noop(self):
        once = encrypt("Nederlandse")
        assert encrypt(once) == once

    def test_still_decrypts_after_double_call(self):
        assert decrypt(encrypt(encrypt("AB1234567"))) == "AB1234567"

    def test_empty_stays_empty(self):
        assert encrypt("") == ""


class TestProfileEncryptionMigration:
    @pytest.fixture(autouse=True)
    def _fresh(self):
        from mstorage._base import StorageBase
        StorageBase._migrated_paths.clear()
        yield
        StorageBase._migrated_paths.clear()

    def _seed(self, path, profile):
        from mstorage import Storage
        st = Storage(path)
        st.conn.execute(
            "INSERT INTO user_configs (id, name, enabled, auto_book_json,"
            " created_at, updated_at) VALUES (?,?,1,?,?,?)",
            ("u1", "t", json.dumps({"applicant_profile": profile}, ensure_ascii=False),
             "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        )
        st.conn.commit(); st.close()

    def _reopen_profile(self, path):
        from mstorage import Storage
        from mstorage._base import StorageBase
        StorageBase._migrated_paths.clear()
        st = Storage(path)
        row = st.conn.execute(
            "SELECT auto_book_json FROM user_configs WHERE id='u1'").fetchone()
        st.close()
        return json.loads(row["auto_book_json"])["applicant_profile"]

    def test_plaintext_at_rest_gets_encrypted(self, tmp_path):
        p = tmp_path / "listings.db"
        self._seed(p, {"criminal_charges": "No", "nationality": "Chinese",
                       "no_middle_name": True})
        got = self._reopen_profile(p)
        assert got["criminal_charges"].startswith("$F$")
        assert got["nationality"].startswith("$F$")
        assert got["no_middle_name"] is True

    def test_values_survive_the_migration(self, tmp_path):
        p = tmp_path / "listings.db"
        self._seed(p, {"id_number": "AB1234567"})
        assert decrypt(self._reopen_profile(p)["id_number"]) == "AB1234567"

    def test_already_encrypted_is_left_alone(self, tmp_path):
        """幂等：跑第二遍不能套第二层。"""
        p = tmp_path / "listings.db"
        self._seed(p, {"id_number": encrypt("AB1234567")})
        first = self._reopen_profile(p)["id_number"]
        assert self._reopen_profile(p)["id_number"] == first
        assert decrypt(first) == "AB1234567"

    def test_second_run_writes_nothing(self, tmp_path):
        """已经加密过的行不该被反复重写。

        encrypt() 幂等之后，少了「跳过已加密」那道判断结果也一样对，但每次启动
        都会对每一行发一次无谓的 UPDATE。这条用例守的是**没有多余写入**，
        不是结果正确——后者已经被上一条守着了。

        用 SQLite 自带的 ``total_changes``（本连接迄今改过多少行）来数：
        caplog 依赖日志器配置，整套跑时别的用例改过 propagate 就会静默失效
        （第一版这么挂过）；而 sqlite3.Connection 是 C 类型，方法没法 monkeypatch
        （第二版这么挂过）。
        """
        from mstorage import Storage
        from mstorage._base import StorageBase

        p = tmp_path / "listings.db"
        self._seed(p, {"id_number": "AB1234567"})

        StorageBase._migrated_paths.clear()
        st1 = Storage(p)
        first = st1.conn.total_changes
        st1.close()
        assert first > 0, "第一次开库应当真的迁移了"

        StorageBase._migrated_paths.clear()
        st2 = Storage(p)
        second = st2.conn.total_changes
        st2.close()
        assert second == 0, f"第二次开库仍然写了 {second} 行"

    def test_broken_json_does_not_block_startup(self, tmp_path):
        from mstorage import Storage
        from mstorage._base import StorageBase
        p = tmp_path / "listings.db"
        st = Storage(p)
        st.conn.execute(
            "INSERT INTO user_configs (id, name, enabled, auto_book_json,"
            " created_at, updated_at) VALUES (?,?,1,?,?,?)",
            ("u1", "t", "{not json", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
        st.conn.commit(); st.close()
        StorageBase._migrated_paths.clear()
        Storage(p).close()          # 不许抛
