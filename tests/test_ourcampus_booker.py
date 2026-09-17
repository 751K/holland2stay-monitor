"""OurCampusBooker：与 OurDomain 同一套流程，只做到「开始申请」。

它**没有注册**进 ``BOOKER_REGISTRY``——「开始申请能占住单元」这个前提还没实测
（见 ``bookers/rentcafe.py`` 里 ``OurCampusBooker`` 的说明）。这里钉的是已经
有实证的部分：OC 的单元表只认 POST、按钮文字决定能不能开始、终点停在
Applicant Info 且不碰证件和保存。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from bookers.base import BookingRequest
from bookers.rentcafe import OurCampusBooker, OurDomainBooker
from bookers.rentcafe_units import find_unit, parse_apply_now_options
from config import ApplicantProfile, AutoBookConfig
from models import Listing
from scrapers.ourcampus import OurCampusScraper
from tests.test_ourdomain_booker import _FakeSession as _ODFakeSession
from users import UserConfig

#: 生产留档原样取出的 availableunits 响应（fp=1113259，单元 #2301，Join Lottery）。
LOTTERY_UNITS = (
    Path(__file__).parent / "fixtures" / "ourcampus_availableunits_lottery.html"
).read_text(encoding="utf-8")
#: 同一份响应，按钮换成 08-27 实测的 Book Now——两者只差这几个字。
BOOK_NOW_UNITS = LOTTERY_UNITS.replace("Join Lottery", "Book Now")

CITY = OurCampusScraper.BUILDINGS["diemen"]["display"]
OC_HOST = "new-ourcampus-amsterdam-diemen-rentcafewebsiteuk.securerc.co.uk"


def _user(*, email="me@oc.nl", password="pw", profile=None, consent="") -> UserConfig:
    return UserConfig(id="u1", name="U", auto_book=AutoBookConfig(
        enabled=True, dry_run=False,
        ourcampus_email=email, ourcampus_password=password,
        applicant_profile=profile or ApplicantProfile(),
        screening_consent_at=consent,
    ))


def _listing(lid="oc_456556", city=CITY) -> Listing:
    return Listing(
        id=lid, name="OurCampus Diemen #2301", status="Available to book",
        price_raw="€ 823", available_from="2026-10-08", features=[],
        url=f"https://{OC_HOST}/onlineleasing/new-ourcampus-amsterdam-diemen/floorplans.aspx",
        city=city, source="ourcampus",
    )


class _FakeSession(_ODFakeSession):
    def __init__(self, api_key, source="ourcampus"):
        super().__init__(api_key, source)
        _FakeSession.instances.append(self)   # 父类记在它自己的列表上
        self.units_html = BOOK_NOW_UNITS
        self.posted_units = []

    def ole_url(self, page):
        return f"https://{OC_HOST}/onlineleasing/new-ourcampus-amsterdam-diemen/{page}"

    def content_url(self, query):
        return f"https://{OC_HOST}/onlineleasing/rcLoadContent.ashx?{query}"

    def open(self, url):
        super().open(url)
        # OC 的 floorplans 页：三个 floorplan id 都在 showDialog 里
        self._html = "".join(
            f"<a onclick=\"showDialog('x','photogallery','subPointerId={fp}&x=1');\">g</a>"
            for fp in ("1113259", "1112904", "1112905")
        )
        return {}

    def fetch(self, url, *, referer="", ajax=False):
        if "availableunits" in url:
            raise AssertionError("OC 的单元表不能走 GET——抓取侧实测只认 POST + floorPlans[]")
        return super().fetch(url, referer=referer, ajax=ajax)

    def fetch_post(self, url, data, *, referer="", ajax=False):
        self.calls.append(f"fetch_post:{'ajax' if ajax else 'plain'}")
        self.posted_units.append((url, list(data)))
        assert ajax, "rcLoadContent.ashx 缺 X-Requested-With 一律 403"
        return self.units_html


@pytest.fixture(autouse=True)
def _reset():
    _FakeSession.instances = []
    yield
    _FakeSession.instances = []


def _book(listing=None, user=None, session_cls=_FakeSession, booker_cls=OurCampusBooker):
    req = BookingRequest(listing=listing or _listing(), user=user or _user())
    with patch("bookers.rentcafe.RentCafeSession", session_cls), \
         patch("applicant_docs.load", lambda uid: ("p.pdf", b"x")):
        return booker_cls().book(req)


# ── 解析：OC 的真实行 ────────────────────────────────────────────────

class TestParsingTheRealRow:
    def test_book_now_row_yields_the_callback_arguments(self):
        [u] = parse_apply_now_options(BOOK_NOW_UNITS, id_prefix="oc_")
        assert (u.unit_id, u.floor_plan_id, u.property_id, u.available_date, u.next_url) == (
            "456556", "1112904", "186609", "8-10-2026", "termsandotheritems.aspx")
        assert u.listing_id == "oc_456556"

    def test_prefix_is_passed_not_hardcoded(self):
        """前缀写死成 od_ 时 listing_id 是 od_456556——日志和结果里的房源 id 全错。"""
        [u] = parse_apply_now_options(BOOK_NOW_UNITS, id_prefix="oc_")
        assert u.id_prefix == "oc_"
        assert find_unit(BOOK_NOW_UNITS, "oc_456556", id_prefix="oc_",
                         parser=parse_apply_now_options) is not None


# ── 平台钩子 ─────────────────────────────────────────────────────────

class TestPlatformHooks:
    def test_id_prefix_matches_the_scraper(self):
        assert OurCampusBooker.id_prefix == OurCampusScraper.ID_PREFIX

    def test_building_resolves_from_ourcampus_metadata(self):
        b = OurCampusBooker()
        assert b._building_key(_listing()) == "diemen"
        assert OurDomainBooker()._building_key(_listing()) == "", (
            "OurDomain 的楼栋表里不该认得 OC 的楼")

    def test_entry_is_the_oc_host_not_ourdomain(self):
        """拿错 BASE 会登录到 OurDomain 的门户上去开申请。"""
        b = OurCampusBooker()
        url = b._floorplans_url(b._building(_listing()))
        assert url == f"https://{OC_HOST}/onlineleasing/new-ourcampus-amsterdam-diemen/floorplans.aspx"

    def test_account_comes_from_the_ourcampus_fields(self):
        ab = AutoBookConfig(ourdomain_email="od@x", ourdomain_password="od",
                            ourcampus_email="oc@x", ourcampus_password="oc")
        assert OurCampusBooker()._account_for(ab, "diemen") == ("oc@x", "oc")

    def test_ourdomain_credentials_do_not_leak_into_ourcampus(self):
        ab = AutoBookConfig(ourdomain_email="od@x", ourdomain_password="od")
        r = _book(user=UserConfig(id="u", name="U", auto_book=ab))
        assert r.phase == "not_configured"
        assert _FakeSession.instances == [], "没凭据还触网了"


# ── 流程 ─────────────────────────────────────────────────────────────

class TestStopsAtApplicantInfo:
    def test_result_is_application_started(self):
        r = _book()
        assert (r.success, r.phase) == (True, "application_started")

    def test_never_uploads_or_saves(self):
        """终点是 Applicant Info：传证件、存草稿都不做。"""
        _book()
        assert _FakeSession.instances[0].calls == [
            "open", "fetch_post:ajax", "book-now:456556", "submit_terms", "login:me@oc.nl",
        ]

    def test_units_are_fetched_with_the_scrapers_post_shape(self):
        _book()
        url, data = _FakeSession.instances[0].posted_units[0]
        assert url.endswith("rcLoadContent.ashx?contentclass=availableunits")
        assert data == [("floorPlans[]", "1113259")]

    def test_no_profile_consent_or_documents_needed(self):
        """档案/授权/证件是给「填表 + 保存」用的；不填表还要求它们只会误挡。"""
        r = _book(user=_user(profile=ApplicantProfile(), consent=""))
        assert r.phase == "application_started"

    def test_ourdomain_still_requires_them(self):
        """钩子只放开 OC——OurDomain 照旧要档案，否则会提交空白申请。"""
        from tests.test_ourdomain_booker import _FakeSession as ODSession, _listing as od_listing
        u = UserConfig(id="u", name="U", auto_book=AutoBookConfig(
            enabled=True, dry_run=False, ourdomain_email="a", ourdomain_password="b"))
        r = _book(listing=od_listing(), user=u, session_cls=ODSession, booker_cls=OurDomainBooker)
        assert r.phase == "not_configured"

    def test_pay_url_is_the_application_entry(self):
        r = _book()
        assert r.pay_url.endswith("new-ourcampus-amsterdam-diemen/oleapplication.aspx")
        assert r.contract_start_date == "8-10-2026"

    def test_message_does_not_claim_the_unit_is_held(self):
        """开始申请能不能占住单元没验证过——说「锁定了」用户就不急着去填。"""
        msg = _book().message
        for claim in ("锁定", "锁住", "已占住", "抢到", "到手"):
            assert claim not in msg, f"文案声称了未验证的事：{claim}"
        assert "立刻" in msg and "什么都还没提交" in msg

    def test_dry_run_does_not_touch_the_network(self):
        u = _user()
        u.auto_book.dry_run = True
        r = _book(user=u)
        assert r.phase == "dry_run" and "开始申请" in r.message
        assert _FakeSession.instances == []


class TestLotteryIsNotStarted:
    def _lottery_session(self):
        class S(_FakeSession):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self.units_html = LOTTERY_UNITS
        return S

    def test_join_lottery_is_refused_before_the_terms_page(self):
        """点下去是加入抽签，不是用户授权的动作。"""
        r = _book(session_cls=self._lottery_session())
        assert r.phase == "unsupported" and r.success is False
        assert "抽签" in r.message
        calls = _FakeSession.instances[0].calls
        assert not any(c.startswith(("book-now", "submit_terms", "login")) for c in calls), calls

    def test_it_is_not_race_lost(self):
        """race_lost 会进重试队列反复试——抽签重试多少次都一样。"""
        assert _book(session_cls=self._lottery_session()).phase != "race_lost"

    def test_missing_unit_is_race_lost(self):
        r = _book(listing=_listing(lid="oc_999999"))
        assert r.phase == "race_lost"
        assert not any(c.startswith("book-now") for c in _FakeSession.instances[0].calls)


# ── 单元表 403：冷却指纹、重开会话 ─────────────────────────────────

class TestUnitTable403RotatesFingerprint:
    """2026-09-15 OC 实测：chrome124 打开页面 200，POST 单元表连续 403；chrome136 200。"""

    POOL = ["chrome124", "chrome136", "safari18_0", "firefox135"]

    def _session(self, blocked: set):
        pool = self.POOL

        class S(_FakeSession):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self.opens = 0
                self.impersonate = ""

            def open(self, url):
                self.impersonate = pool[self.opens % len(pool)]
                self.opens += 1
                return super().open(url)

            def fetch_post(self, url, data, *, referer="", ajax=False):
                if self.impersonate in blocked:
                    self.calls.append(f"403:{self.impersonate}")
                    from bookers.rentcafe import RentCafeBlockedError
                    raise RentCafeBlockedError("403 on POST")
                return super().fetch_post(url, data, referer=referer, ajax=ajax)
        return S

    def _run(self, blocked):
        cooled = []
        with patch("scrapers.ourdomain._mark_fingerprint_blocked", cooled.append):
            r = _book(session_cls=self._session(blocked))
        return r, cooled, _FakeSession.instances[0]

    def test_403_cools_the_fingerprint_and_reopens(self):
        r, cooled, s = self._run({"chrome124"})
        assert r.phase == "application_started"
        assert cooled == ["chrome124"], "不记冷却的话下一次 open() 还会先拿它"
        assert s.opens == 2
        assert s.calls[:4] == ["open", "403:chrome124", "open", "fetch_post:ajax"]

    def test_gives_up_as_blocked_after_bounded_reopens(self):
        r, cooled, s = self._run(set(self.POOL))
        assert r.phase == "blocked" and r.success is False
        assert s.opens == OurCampusBooker._UNITS_403_REOPENS + 1, "重开次数必须有上限"
        assert cooled == self.POOL[: s.opens]
        assert "chrome124" in r.message

    def test_does_not_reach_the_terms_page_while_blocked(self):
        _, _, s = self._run(set(self.POOL))
        assert not any(c.startswith(("book-now", "submit_terms", "login")) for c in s.calls)


# ── 未注册 ───────────────────────────────────────────────────────────

def test_not_registered_until_the_hold_is_verified():
    """注册 = 用户够得着。「开始申请能占住单元」没实测之前不注册。

    这条挂了说明有人注册了它——请先确认 bookers/rentcafe.py 里 OurCampusBooker
    说明的两项验证已经做过，再改这条测试、补面板入口。
    """
    from bookers import BOOKER_REGISTRY
    assert "ourcampus" not in BOOKER_REGISTRY


# ── 凭据存取 ─────────────────────────────────────────────────────────

class TestCredentialStorage:
    def test_password_is_encrypted_at_rest(self):
        import json

        from crypto import decrypt
        from users import _user_to_row

        u = UserConfig(name="t")
        u.auto_book = AutoBookConfig(ourcampus_email="oc@x", ourcampus_password="pw")
        ab = json.loads(_user_to_row(u)["auto_book_json"])
        assert ab["ourcampus_password"] != "pw", "明文进库了"
        assert decrypt(ab["ourcampus_password"]) == "pw"

    def test_load_decrypts(self):
        from crypto import encrypt
        from users import _ab_from_dict

        ab = _ab_from_dict({"ourcampus_email": "oc@x", "ourcampus_password": encrypt("pw")})
        assert (ab.ourcampus_email, ab.ourcampus_password) == ("oc@x", "pw")

    def test_saving_the_panel_form_keeps_them(self, admin_client):
        """面板还没有 OC 输入框。表单每次保存都重建 AutoBookConfig——不显式保留的话，
        管理员随手改一次别的设置，OC 凭据就被清成空串。"""
        from users import load_users, save_users

        users = load_users()
        u = UserConfig(name="oc-keep")
        u.auto_book = AutoBookConfig(ourcampus_email="oc@x", ourcampus_password="pw")
        save_users(users + [u])
        uid = next(x.id for x in load_users() if x.name == "oc-keep")

        r = admin_client.post(f"/users/{uid}", data={
            "name": "oc-keep", "csrf_token": "test_csrf",
        }, headers={"X-CSRF-Token": "test_csrf"}, follow_redirects=True)
        assert r.status_code == 200

        got = next(x for x in load_users() if x.id == uid).auto_book
        assert (got.ourcampus_email, got.ourcampus_password) == ("oc@x", "pw")


# ── 条款页说明 ───────────────────────────────────────────────────────

class TestTermsPageNotices:
    #: 2026-09-15 OC #1036（抽签）条款页上的原句。
    LOTTERY_NOTE = ("Please note: submitting this application enters you into the lottery "
                    "for this unit; it does not reserve the apartment.")

    def test_picks_the_real_lottery_sentence(self):
        from bookers.rentcafe import terms_page_notices
        html = f"<div class='x'><p><strong>{self.LOTTERY_NOTE}</strong></p></div>"
        assert terms_page_notices(html) == [self.LOTTERY_NOTE]

    def test_ignores_scripts_and_unrelated_text(self):
        from bookers.rentcafe import terms_page_notices
        html = ("<script>var lottery = 'reserve';</script><p>Welcome to OurCampus.</p>"
                "<p>We will hold your deposit until move-in.</p>")
        assert terms_page_notices(html) == ["We will hold your deposit until move-in."]

    def test_entities_decoded_and_deduplicated(self):
        from bookers.rentcafe import terms_page_notices
        html = "<p>It doesn&#39;t reserve the unit.</p>" * 2
        assert terms_page_notices(html) == ["It doesn't reserve the unit."]

    def test_logged_during_a_real_run(self, caplog):
        """条款页说明必须进日志——自动跑的时候没人看页面。"""
        import logging

        class S(_FakeSession):
            def open_terms_for_unit(self, unit, *, referer=""):
                html = super().open_terms_for_unit(unit, referer=referer)
                return html + f"<p>{TestTermsPageNotices.LOTTERY_NOTE}</p>"

        with caplog.at_level(logging.INFO, logger="bookers.rentcafe"):
            _book(session_cls=S)
        assert any("does not reserve" in r.getMessage() for r in caplog.records)


# ── 服务端消息 ───────────────────────────────────────────────────────

class TestServerMessages:
    """RENTCafe 用一段 JS 说话，不用 HTTP 状态码，正文里可能一个字都没有。

    2026-09-17 OurCampus 实测：登录后重选单元被拒，整个响应 1237 字节，全部信息
    都在 ``$.showMessage({...text:"..."})`` 里。不解析它，用户只会看到
    「不知道为什么失败」。
    """

    #: 生产响应里原样抄来的片段。
    REAL = (
        '<script type="text/javascript">(function($) {$(function () {\n'
        'DecodeFormElementsToBase64(); \n'
        '$.showMessage({type: "error",text:"Unit is not available. '
        'Please select another unit.",time:5000,slideTime:500,position:\'top\', '
        "backgroundColor:'#CC3300', width:210, id:''});\n});\n})(jQuery);\n</script>"
    )

    def test_reads_the_real_message(self):
        from bookers.rentcafe import server_messages
        assert server_messages(self.REAL) == [
            "Unit is not available. Please select another unit."]

    def test_no_message_is_empty_not_an_error(self):
        from bookers.rentcafe import server_messages
        assert server_messages("<p>hello</p>") == []
        assert server_messages("") == []

    def test_deduplicates_and_decodes_entities(self):
        from bookers.rentcafe import server_messages
        html = '$.showMessage({type:"error",text:"It&#39;s gone"});' * 2
        assert server_messages(html) == ["It's gone"]

    def test_failure_message_quotes_the_server(self):
        """登录后落错页时，错误消息里必须有服务端的原话。"""
        class S(_FakeSession):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self.applicant_html = TestServerMessages.REAL

            def submit_terms(self, fields, *, move_in_date="", page="oleapplication"):
                super().submit_terms(fields, move_in_date=move_in_date, page=page)
                self._html = TestServerMessages.REAL
                return self._html

        r = _book(session_cls=S)
        assert r.phase == "unknown_error"
        assert "Unit is not available" in r.message, r.message
