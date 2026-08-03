"""Xior 半自动预订 book() 测试。

半自动的边界是产品承诺，不只是实现细节：booker 走到 **Save**（存草稿）就停，
上传证件和付款留给用户。所以：

- 返回 phase 必须是 ``draft_saved`` 而不是 ``success``——把它当成功报给用户，
  用户会以为房到手了、慢悠悠去传证件，结果被别人抢走；
- 通知文案必须让人知道要**赶紧**去传证件。

另外前置校验必须在**触网之前**完成：RENTCafe 连续失败会锁 30 分钟，缺凭据
或缺档案时拿一次注定失败的请求去撞，等于消耗真正需要它的额度。
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from bookers.base import BookingRequest
from bookers.rentcafe import XiorBooker
from config import ApplicantProfile, AutoBookConfig
from models import Listing
from users import UserConfig

BUILDING = "p0196062"
DISPLAY = "Amsterdam Karspeldreef"


def _profile(**over) -> ApplicantProfile:
    base = dict(
        first_name="J", last_name="Kong", no_middle_name=True, gender="Male",
        date_of_birth="2003-09-14", nationality="China", country="Netherlands",
        address="Dorpsstraat 1", postcode_city="5612 AB Eindhoven",
        university="TU Eindhoven", place_of_birth="China", id_number="E12345678",
    )
    base.update(over)
    return ApplicantProfile(**base)


def _user(*, accounts=None, profile=None, dry_run=False, consent=True) -> UserConfig:
    return UserConfig(id="u1", name="U", auto_book=AutoBookConfig(
        enabled=True, dry_run=dry_run,
        screening_consent_at="2026-08-03T20:00:00+00:00" if consent else "",
        xior_accounts=accounts if accounts is not None
        else {BUILDING: {"email": "a@x.com", "password": "pw"}},
        applicant_profile=profile if profile is not None else _profile(),
    ))


def _listing(lid="xr_398336", city=DISPLAY) -> Listing:
    return Listing(
        id=lid, name="Karspeldreef 1.S127", status="Available to book",
        price_raw="€499", available_from="2026-08-16", features=[],
        url="https://h.securerc.co.uk/onlineleasing/p/oleapplication.aspx?x=1",
        city=city, source="xior",
    )


def _req(**kw) -> BookingRequest:
    return BookingRequest(listing=kw.pop("listing", _listing()),
                          user=kw.pop("user", _user()), **kw)


class _FakeSession:
    """记录调用顺序；默认选房页里有目标单元。"""

    instances: list = []

    def __init__(self, api_key):
        self.calls: list[str] = []
        self.unit_html = (
            '<button class="UnitSelect" name="1.S127" '
            "onclick=\"ContinueClick('398336','1111515','185795','16-8-2026','',"
            "'oleapplication.aspx?stepname=ApplicantInfo')\">Reserve this room</button>"
        )
        self.saved_fields = None
        _FakeSession.instances.append(self)

    def open(self, url):
        self.calls.append("open")
        return {"formName2": "termsandotheritems"}

    def submit_terms(self, fields, **kw):
        self.calls.append("submit_terms")
        return ""

    def login(self, email, password):
        self.calls.append(f"login:{email}")
        return {}

    def current_page_html(self):
        return self.unit_html

    def select_unit(self, unit):
        self.calls.append(f"select:{unit.unit_id}")
        return "<html>applicant info</html>"

    def save_applicant_info(self, html, fields):
        self.calls.append("save")
        self.saved_fields = fields
        return ""


@pytest.fixture(autouse=True)
def _reset():
    _FakeSession.instances = []
    yield
    _FakeSession.instances = []


def _book(request, session_cls=_FakeSession):
    with patch("bookers.rentcafe.RentCafeSession", session_cls):
        return XiorBooker().book(request)


# ── 前置校验（不触网）────────────────────────────────────────────────


class TestPreflight:
    def test_missing_credentials_skips_without_network(self):
        r = _book(_req(user=_user(accounts={})))
        assert r.success is False and r.phase == "not_configured"
        assert _FakeSession.instances == [], "不该建立会话"

    def test_credentials_for_another_building_do_not_count(self):
        """拿 A 楼账号去 B 楼登录必然失败，还会烧掉 IP 尝试额度。"""
        r = _book(_req(user=_user(accounts={"p9999": {"email": "a@x", "password": "p"}})))
        assert r.phase == "not_configured"
        assert _FakeSession.instances == []

    def test_incomplete_profile_skips_without_network(self):
        r = _book(_req(user=_user(profile=_profile(university=""))))
        assert r.phase == "not_configured"
        assert "university" in r.message
        assert _FakeSession.instances == []

    def test_unknown_building_is_unsupported(self):
        r = _book(_req(listing=_listing(city="Nowhere Street")))
        assert r.phase == "unsupported"
        assert _FakeSession.instances == []

    def test_dry_run_does_not_touch_network(self):
        r = _book(_req(user=_user(dry_run=True)))
        assert r.success is True and r.phase == "dry_run" and r.dry_run is True
        assert _FakeSession.instances == []


# ── 正常路径 ────────────────────────────────────────────────────────


class TestHappyPath:
    def test_walks_the_flow_in_order(self):
        """选房上下文存在服务端会话里，必须从 applyOnlineURL 依次走。"""
        _book(_req())
        s = _FakeSession.instances[0]
        assert s.calls == ["open", "submit_terms", "login:a@x.com",
                           "select:398336", "save"]

    def test_result_is_draft_not_success(self):
        """phase 必须区分开：它没占住房，只是把申请填好了。"""
        r = _book(_req())
        assert r.success is True
        assert r.phase == "draft_saved"
        assert r.phase != "success"

    def test_message_tells_user_to_hurry(self):
        r = _book(_req())
        assert "上传证件" in r.message
        assert "迅速" in r.message or "尽快" in r.message

    def test_message_does_not_claim_the_unit_is_secured(self):
        """说成「已抢到」会让用户慢悠悠去传证件，然后被别人抢走。"""
        for banned in ("已抢到", "已预订成功", "已为你占", "预订成功"):
            assert banned not in _book(_req()).message

    def test_profile_fields_are_submitted(self):
        _book(_req())
        fields = _FakeSession.instances[0].saved_fields
        assert fields["FirstName"] == "J"
        assert fields["DateOfBirth"] == "14-9-2003"      # 转成 d-m-yyyy 不补零
        assert fields["University"] == "TU Eindhoven"

    def test_move_in_date_comes_from_the_unit(self):
        _book(_req())
        assert _FakeSession.instances[0].saved_fields["MoveInDate"] == "16-8-2026"


# ── 失败路径 ────────────────────────────────────────────────────────


class TestFailures:
    def test_unit_gone_is_race_lost(self):
        """race_lost 让上层换备选房源继续尝试，而不是整个放弃。"""
        class _NoUnit(_FakeSession):
            def __init__(self, k):
                super().__init__(k)
                self.unit_html = "<html>no units</html>"
        r = _book(_req(), _NoUnit)
        assert r.success is False and r.phase == "race_lost"

    def test_blocked_is_reported_as_blocked(self):
        from bookers.rentcafe import RentCafeBlockedError

        class _Blocked(_FakeSession):
            def login(self, *a, **k):
                raise RentCafeBlockedError("403")
        r = _book(_req(), _Blocked)
        assert r.phase == "blocked"

    def test_unexpected_error_does_not_escape(self):
        """booker 契约：失败返回 BookingResult，不抛异常。"""
        class _Boom(_FakeSession):
            def open(self, url):
                raise RuntimeError("boom")
        r = _book(_req(), _Boom)
        assert r.success is False and r.phase == "unknown_error"

    def test_never_advances_past_save(self):
        """上传证件和付款不该由系统代劳——流程里不能出现 Next/付款调用。"""
        _book(_req())
        s = _FakeSession.instances[0]
        assert not any("next" in c.lower() or "pay" in c.lower() for c in s.calls)


class TestScreeningConsent:
    """代勾法律声明必须有用户预先授权，且授权要能追溯到时刻。"""

    def test_without_consent_nothing_is_submitted(self):
        r = _book(_req(user=_user(consent=False)))
        assert r.success is False and r.phase == "not_configured"
        assert "授权" in r.message
        assert _FakeSession.instances == [], "没授权就不该触网"

    def test_agreement_is_ticked_only_with_consent(self):
        from bookers.rentcafe_applicant import AGREEMENT_FIELD
        _book(_req())
        assert _FakeSession.instances[0].saved_fields[AGREEMENT_FIELD] == "on"

    def test_id_number_reaches_the_form(self):
        _book(_req())
        f = _FakeSession.instances[0].saved_fields
        assert f["IDNumber"] == "E12345678"
        assert f["PlaceOfBirth"] == "China"
