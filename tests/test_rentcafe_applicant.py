"""申请表字段映射测试。

映射错位的后果不是报错，是**默默提交一份填错的申请**——所以这一层要用测试
钉死，而不是靠跑一次看着像对的。

字段名来自 2026-08-03 对真实页面（已登录的 Vaals / Katzensprung）的实测。
"""
from __future__ import annotations

import pytest

from bookers.rentcafe_applicant import (
    ANTI_BOT_FIELDS,
    DOB_FIELD,
    FIELD_MAP,
    MOVE_IN_FIELD,
    NO_MIDDLE_NAME_FIELD,
    ProfileIncompleteError,
    build_form_fields,
    carry_over_fields,
    from_rentcafe_date,
    to_rentcafe_date,
)
from config import ApplicantProfile


def _full(**over) -> ApplicantProfile:
    base = dict(
        title="Mr.", first_name="J", last_name="Kong", no_middle_name=True,
        phone="+31600", gender="Male", date_of_birth="2003-09-14",
        nationality="China", country="Netherlands", address="Dorpsstraat 1",
        postcode_city="5612 AB Eindhoven", university="TU Eindhoven",
        min_lease_term="12",
    )
    base.update(over)
    return ApplicantProfile(**base)


class TestDateFormat:
    @pytest.mark.parametrize("iso,want", [
        ("2026-08-03", "3-8-2026"),
        ("2003-09-14", "14-9-2003"),
        ("2026-12-25", "25-12-2026"),
    ])
    def test_iso_to_rentcafe_does_not_zero_pad(self, iso, want):
        """实测页面上是 3-8-2026 而不是 03-08-2026。补零的串没试过，
        与其赌它也能被接受，不如照抄观察到的形状。"""
        assert to_rentcafe_date(iso) == want

    @pytest.mark.parametrize("bad", ["", "not-a-date", "3-8-2026", "2026/08/03"])
    def test_unparseable_passes_through(self, bad):
        assert to_rentcafe_date(bad) == bad

    def test_round_trip(self):
        assert from_rentcafe_date(to_rentcafe_date("2003-09-14")) == "2003-09-14"

    @pytest.mark.parametrize("bad", ["", "x", "1-2", "99-99-9999"])
    def test_reverse_tolerates_garbage(self, bad):
        assert from_rentcafe_date(bad) == bad


class TestFieldMapping:
    def test_maps_every_profile_field(self):
        got = build_form_fields(_full())
        assert got[FIELD_MAP["first_name"]] == "J"
        assert got[FIELD_MAP["last_name"]] == "Kong"
        assert got[FIELD_MAP["gender"]] == "Male"
        assert got[FIELD_MAP["nationality"]] == "China"
        assert got[FIELD_MAP["university"]] == "TU Eindhoven"
        assert got[FIELD_MAP["postcode_city"]] == "5612 AB Eindhoven"

    def test_dob_is_converted(self):
        assert build_form_fields(_full())[DOB_FIELD] == "14-9-2003"

    def test_values_are_stripped(self):
        got = build_form_fields(_full(university="  TU  "))
        assert got[FIELD_MAP["university"]] == "TU"

    def test_move_in_date_is_optional(self):
        assert MOVE_IN_FIELD not in build_form_fields(_full())
        got = build_form_fields(_full(), move_in_date="16-8-2026")
        assert got[MOVE_IN_FIELD] == "16-8-2026"


class TestMiddleName:
    def test_no_middle_name_sets_flag_and_clears_value(self):
        got = build_form_fields(_full(no_middle_name=True, middle_name="ignored"))
        assert got[NO_MIDDLE_NAME_FIELD] == "true"
        assert got[FIELD_MAP["middle_name"]] == ""

    def test_middle_name_present_clears_flag(self):
        got = build_form_fields(_full(no_middle_name=False, middle_name="Q"))
        assert got[NO_MIDDLE_NAME_FIELD] == "false"
        assert got[FIELD_MAP["middle_name"]] == "Q"


class TestCompletenessGate:
    def test_incomplete_profile_raises(self):
        """填一半的表单提交不上去，只会消耗 RENTCafe 的尝试额度（连续失败锁
        30 分钟），还在用户账号下留一条废弃申请。"""
        with pytest.raises(ProfileIncompleteError) as e:
            build_form_fields(_full(university=""))
        assert "university" in str(e.value)

    def test_strict_false_is_for_preview_only(self):
        got = build_form_fields(_full(university=""), strict=False)
        assert got[FIELD_MAP["university"]] == ""

    def test_missing_middle_name_decision_blocks(self):
        with pytest.raises(ProfileIncompleteError):
            build_form_fields(_full(middle_name="", no_middle_name=False))


class TestAntiBotFields:
    def test_carries_over_only_known_fields(self):
        page = {"txtCodeVal": "abc", "txtRenderTime": "3-8-2026 15:12:16",
                "txtvalue1": "tok", "txtvalue2": "", "SomethingElse": "x"}
        got = carry_over_fields(page)
        assert set(got) == set(ANTI_BOT_FIELDS)
        assert got["txtRenderTime"] == "3-8-2026 15:12:16"

    def test_honeypot_stays_empty(self):
        """txtvalue2 是空 textarea，疑似蜜罐——填上值多半会被判成机器人。"""
        got = carry_over_fields({"txtvalue2": ""})
        assert got["txtvalue2"] == ""

    def test_absent_fields_are_not_invented(self):
        assert carry_over_fields({}) == {}

    def test_render_time_is_carried_not_regenerated(self):
        """页面渲染时刻必须原样回传——自己造一个「现在」等于告诉服务端
        这份表单是零秒填完的。"""
        assert "txtRenderTime" in ANTI_BOT_FIELDS
        page = {"txtRenderTime": "3-8-2026 15:12:16"}
        assert carry_over_fields(page)["txtRenderTime"] == "3-8-2026 15:12:16"
