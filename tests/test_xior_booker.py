"""Xior 半自动预订 book() 测试。

半自动的边界是产品承诺，不只是实现细节：booker 走到 **Save**（存草稿）就停，
付款留给用户——付款那一步要填银行账号和 SWIFT（2026-08-03 侦察确认），代填
金融凭据是硬限制。证件由系统代传（平台在证件到位前拒绝保存任何内容）。所以：

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
        id_country="China", housing_type="Rent",
        ever_evicted="No", ever_convicted="No", criminal_charges="No",
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


#: 申请表字段名必须从**真实页面**解析（一批名字里嵌着 prospect id），
#: 所以 fake 也得返回一张结构真实的表，否则测的就不是真实路径。
#: 完整字段清单见 tests/test_rentcafe_applicant.py。
from tests.test_rentcafe_applicant import PAGE as APPLICANT_PAGE, PID


class _FakeSession:
    """记录调用顺序；默认选房页里有目标单元。"""

    instances: list = []

    def __init__(self, api_key, source="xior"):
        self.calls: list[str] = []
        self.source = source
        self.unit_html = (
            '<button class="UnitSelect" name="1.S127" '
            "onclick=\"ContinueClick('398336','1111515','185795','16-8-2026','',"
            "'oleapplication.aspx?stepname=ApplicantInfo')\">Reserve this room</button>"
        )
        self.saved_fields = None
        self.uploaded = None
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
        return APPLICANT_PAGE

    def upload_document(self, html, filename, data):
        self.calls.append(f"upload:{filename}")
        self.uploaded = (filename, data)
        return True

    def save_applicant_info(self, html, fields):
        self.calls.append("save")
        self.saved_fields = fields
        return ""


@pytest.fixture(autouse=True)
def _reset():
    _FakeSession.instances = []
    yield
    _FakeSession.instances = []


#: book() 会去读用户的证件文件。测试里用假的，免得碰真实磁盘。
FAKE_DOC = ("passport.pdf", b"%PDF-fake")


def _book(request, session_cls=_FakeSession, doc=FAKE_DOC):
    with patch("bookers.rentcafe.RentCafeSession", session_cls), \
         patch("applicant_docs.load", lambda uid: doc):
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
        r = _book(_req(user=_user(profile=_profile(place_of_birth=""))))
        assert r.phase == "not_configured"
        assert "place_of_birth" in r.message
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
                           "select:398336", "upload:passport.pdf",
                           "select:398336", "save"]

    def test_result_is_draft_not_success(self):
        """phase 必须区分开：它没占住房，只是把申请填好了。"""
        r = _book(_req())
        assert r.success is True
        assert r.phase == "draft_saved"
        assert r.phase != "success"

    def test_message_points_at_payment_with_the_link(self):
        """证件现在由系统代传，用户要做的只剩付款——文案必须指向那一步。

        2026-08-03 侦察确认锁定就在付款：ApplicationCharges 的
        PaymentApplicationChargeStep1 要填 IBAN/SWIFT，页面原文「all
        administration fees will need to be paid」。
        """
        r = _book(_req())
        assert "付款" in r.message
        assert "抢先" in r.message, "要让人知道慢了会没"
        assert r.listing.url in r.message, "付款链接必须在正文里"

    def test_message_does_not_claim_the_unit_is_secured(self):
        """说成「已抢到」会让用户慢悠悠去传证件，然后被别人抢走。"""
        for banned in ("已抢到", "已预订成功", "已为你占", "预订成功"):
            assert banned not in _book(_req()).message

    def test_profile_fields_are_submitted(self):
        _book(_req())
        fields = _FakeSession.instances[0].saved_fields
        assert fields["ProspectFirstName"] == "J"
        assert fields[f"ProspectDOB{PID}"] == "14-9-2003"   # d-m-yyyy 不补零
        assert "FirstName" not in fields, "FirstName 是可见标签，不是字段名"

    def test_move_in_date_is_left_to_the_server(self):
        """入住日字段在页面上是 disabled——jQuery 不序列化它，我们也不该发。

        2026-08-03 实测：PrefMoveinDate{pid} / ProspectEmail / LeaseTerm 三个
        都是 disabled，值由服务端按选中的单元自己算。
        """
        _book(_req())
        fields = _FakeSession.instances[0].saved_fields
        assert f"PrefMoveinDate{PID}" not in fields
        assert "MoveInDate" not in fields


# ── 失败路径 ────────────────────────────────────────────────────────


class TestFailures:
    def test_unit_gone_is_race_lost(self):
        """race_lost 让上层换备选房源继续尝试，而不是整个放弃。"""
        class _NoUnit(_FakeSession):
            def __init__(self, k, source="xior"):
                super().__init__(k, source)
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

    def test_rejected_save_is_never_reported_as_draft_saved(self):
        """服务端拒绝保存时，绝不能告诉用户「已起草申请，请去传证件」。

        2026-08-03 实测踩到：服务端回
        ``$.showMessage({type:"error",text:"Please upload required documents
        before Proceeding."})``，HTTP 仍是 200，而 ``save_applicant_info()``
        压根没检查响应体，于是 ``book()`` 返回 ``draft_saved / success=True``。

        这是这条链路上后果最重的一种错法：用户读到「已为你起草申请」，会安心
        去准备证件，而实际上一个字都没存下——等他发现时房子已经没了。
        """
        from bookers.rentcafe import RentCafeSaveRejectedError

        class _Rejected(_FakeSession):
            def save_applicant_info(self, html, fields):
                self.calls.append("save")
                raise RentCafeSaveRejectedError("申请表保存被 RENTCafe 拒绝：需要证件")

        r = _book(_req(), _Rejected)
        assert r.success is False
        assert r.phase == "save_rejected"
        assert r.phase != "draft_saved"
        assert "没有" in r.message, "必须明说申请没保存"

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
        """实测页面底部是**两个**勾选框，只勾一个照样过不了校验。"""
        from bookers.rentcafe_applicant import AGREEMENT_FIELDS
        _book(_req())
        f = _FakeSession.instances[0].saved_fields
        for name in AGREEMENT_FIELDS:
            assert f[name] == "1"

    def test_id_number_reaches_the_form(self):
        """字段名是 Xior 自定义的那套，``ADDITIOAL`` 的拼写错误来自上游。"""
        _book(_req())
        f = _FakeSession.instances[0].saved_fields
        assert f["STU_IDGUESTCARD_ADDITIOALINFO"] == "E12345678"
        assert f["STU_BIRTHPLACEGUESTCARD_ADDITIOALINFO"] == "China"


class TestDocumentUpload:
    """证件必须在填表**之前**传，否则服务端什么都不保存。

    2026-08-03 实测：ApplicantInfo 页上 ``ID/Passport Upload*`` 是必填，文档没
    到位时保存一律回 ``Please upload required documents before Proceeding.``。
    文档是**按申请**上传的（上传 URL 里带 ProspectID），每抢一个新单元都要重传。
    """

    def test_upload_happens_before_save(self):
        _book(_req())
        calls = _FakeSession.instances[0].calls
        assert calls.index("upload:passport.pdf") < calls.index("save")

    def test_the_stored_document_is_what_gets_sent(self):
        _book(_req())
        assert _FakeSession.instances[0].uploaded == FAKE_DOC

    def test_page_is_refetched_after_upload(self):
        """上传后页面上的隐藏字段会变，拿上传前那份去填表可能已经过期。"""
        calls = _book(_req()) and _FakeSession.instances[0].calls
        assert calls.count("select:398336") == 2

    def test_missing_document_stops_before_touching_the_form(self):
        """没存证件就别提交——注定被拒，还白烧一次 RENTCafe 尝试额度。"""
        r = _book(_req(), doc=None)
        assert r.success is False and r.phase == "not_configured"
        assert "证件" in r.message
        assert "save" not in _FakeSession.instances[0].calls

    def test_failed_upload_does_not_pretend_to_have_applied(self):
        class _BadUpload(_FakeSession):
            def upload_document(self, html, filename, data):
                self.calls.append("upload-failed")
                return False

        r = _book(_req(), _BadUpload)
        assert r.success is False
        assert r.phase != "draft_saved"
        assert "save" not in _FakeSession.instances[0].calls
