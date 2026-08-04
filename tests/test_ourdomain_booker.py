"""OurDomain 半自动预订：入口段 + 平台钩子。

2026-08-04 侦察确认 OurDomain 和 Xior 跑的是同一套 RENTCafe，验证码/登录/表单
契约逐字相同。但 ``OurDomainBooker`` 当时只是 ``source = "ourdomain"`` 一行的
空壳，``book()`` 整个方法体写死 Xior：``building_key_for`` 来自
``scrapers.xior``，对 OurDomain 的城市恒返回空串，于是**第一步就退出**，连一个
请求都发不出去。

这里盯四件事：

1. 平台钩子确实按平台走（不再借 Xior 的楼栋表和 ``xr_`` 前缀）；
2. 入口是 ``ApplyNowClick`` 的参数 + POST ``termsandotheritems.aspx``——
   **不是** ``listing.url``（那是 floorplans 页，进不去申请流程）；
3. 单元参数**现查**，顺带做竞争检测：行没了就是 race_lost；
4. 两栋楼各自的 host 不能串（cookie 不跨主机，串了就是登到另一栋楼的门户）。
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from bookers.base import BookingRequest
from bookers.rentcafe import OurDomainBooker, XiorBooker
from bookers.rentcafe_units import find_unit, parse_apply_now_options
from config import ApplicantProfile, AutoBookConfig
from models import Listing
from scrapers.ourdomain import OurDomainScraper
from users import UserConfig

DIEMEN = "Amsterdam Diemen"
SOUTH_EAST = "Amsterdam South East"

#: 2026-08-04 在 Diemen 实测抄录的 ApplyNowClick（参数已核对）：
#:     ApplyNowClick('211053','1113962','184283','14-9-2026','termsandotheritems.aspx')
#: 行的骨架来自 docs/OURDOMAIN.md §3.2。
UNIT_ROW = """
<tr id="unitrow_211053" data-selenium-id="urow1">
  <th data-selenium-id="Apt1" id="211053">#6031</th>
  <td data-selenium-id="SqFt1" data-label="Sq.M.">22,56</td>
  <td data-selenium-id="Rent1">&euro; 1.138</td>
  <td data-selenium-id="Deposit1">&euro; 2.834</td>
  <td data-selenium-id="Amenity1"><label>Ground Floor</label></td>
  <td data-selenium-id="AvailDate1"><span class="text-success">Available</span></td>
  <td data-selenium-id="Action1">
    <input type="button" value="Book now"
      onclick="ApplyNowClick(&#39;211053&#39;,&#39;1113962&#39;,&#39;184283&#39;,
               &#39;14-9-2026&#39;,&#39;termsandotheritems.aspx&#39;)" />
  </td>
</tr>
"""

UNITS_PAGE = f"<html><body><table>{UNIT_ROW}</table></body></html>"

#: 选中单元之后的条款页。裸 GET 这一页时 UnitId/FloorplanId 是 0，
#: POST 之后才落位——这就是「上下文建起来了」的判据。
TERMS_PAGE = """
<form id="termsandotheritems" name="termsandotheritems"
      action="/onlineleasing/rcformsave.ashx" method="post">
  <input type="hidden" name="formName2" value="termsandotheritems" />
  <input type="hidden" name="UnitId" value="211053" />
  <input type="hidden" name="FloorplanId" value="1113962" />
  <input type="hidden" name="QuotedRent" value="1138.00" />
  <input type="hidden" name="QuotedRentEncr" value="MTEzOC4wMA%3d%3d-RquIlWlxLKs%3d" />
  <input type="hidden" name="myOlePropertyId" value="184283" />
  <input type="hidden" name="g-recaptcha-response-v3" value="" />
  <input type="hidden" name="failed-captcha-3-rentable" value="false" />
</form>
"""

FLOORPLANS_PAGE = """
<html><body>
  <a onclick="showDialog('Floor Plan Superior Studio','photogallery',
     'imagetype=floorplan&amp;subPointerId=1113962&amp;x=1');">gallery</a>
</body></html>
"""

from tests.test_rentcafe_applicant import PAGE as APPLICANT_PAGE


def _profile() -> ApplicantProfile:
    return ApplicantProfile(
        first_name="J", last_name="Kong", no_middle_name=True, gender="Male",
        date_of_birth="2003-09-14", nationality="China", country="Netherlands",
        address="Dorpsstraat 1", postcode_city="5612 AB Eindhoven",
        university="TU Eindhoven", place_of_birth="China", id_number="E12345678",
        id_country="China", housing_type="Rent",
        ever_evicted="No", ever_convicted="No", criminal_charges="No",
    )


def _user(*, email="a@od.com", password="pw") -> UserConfig:
    return UserConfig(id="u1", name="U", auto_book=AutoBookConfig(
        enabled=True, dry_run=False,
        screening_consent_at="2026-08-04T12:00:00+00:00",
        ourdomain_email=email, ourdomain_password=password,
        applicant_profile=_profile(),
    ))


def _listing(lid="od_211053", city=DIEMEN) -> Listing:
    return Listing(
        id=lid, name="Diemen #6031", status="Available to book",
        price_raw="€ 1.138", available_from="2026-09-14", features=[],
        url=("https://thisisourdomain.securerc.co.uk/onlineleasing/"
             "ourdomain-amsterdam-diemen/floorplans.aspx"),
        city=city, source="ourdomain",
    )


# ── 解析层：ApplyNowClick ─────────────────────────────────────────────


class TestApplyNowParsing:
    def test_parses_the_five_arguments(self):
        (opt,) = parse_apply_now_options(UNITS_PAGE)
        assert opt.unit_id == "211053"
        assert opt.floor_plan_id == "1113962"
        assert opt.property_id == "184283"
        assert opt.available_date == "14-9-2026"
        assert opt.next_url == "termsandotheritems.aspx"

    def test_next_url_is_the_fifth_arg_not_the_sixth(self):
        """Xior 的 nextUrl 在第 6 位，OurDomain 在第 5 位。

        位置写错不会报错——只会带着一组错位的参数去提交。
        """
        (opt,) = parse_apply_now_options(UNITS_PAGE)
        assert opt.next_url == "termsandotheritems.aspx"
        assert opt.next_url != "184283"

    def test_listing_id_uses_the_ourdomain_prefix(self):
        (opt,) = parse_apply_now_options(UNITS_PAGE)
        assert opt.listing_id == "od_211053"

    def test_no_school_id(self):
        """SchoolId 是 Xior 学生房专有的，OurDomain 没有这个概念。"""
        (opt,) = parse_apply_now_options(UNITS_PAGE)
        assert opt.school_id == ""

    def test_entities_are_decoded_before_matching(self):
        """页面上 onclick 里的引号是 HTML 实体。

        Xior 那边踩过一次：正则按真引号写，21 个单元一个都没解析出来，流程
        报「已被他人选走」——明明单元就在页面上。
        """
        assert "&#39;" in UNIT_ROW
        assert parse_apply_now_options(UNITS_PAGE), "实体没解码，一个都没解析出来"

    def test_row_without_a_book_button_is_skipped(self):
        """没有「Book now」= 这个单元当前不可订，不是解析失败。"""
        row = UNIT_ROW.replace(
            UNIT_ROW[UNIT_ROW.index("<input type=\"button\""):UNIT_ROW.index("/>") + 2],
            "<span>Wait List</span>",
        )
        assert parse_apply_now_options(f"<table>{row}</table>") == []

    def test_argument_order_change_is_caught_not_guessed(self):
        """第 1 参数和 unitrow 的 id 对不上 = 参数顺序变了，必须跳过。

        猜一个「大概是这个」的位置，后果是在用户账号下申请**别的房号**。
        """
        bad = UNIT_ROW.replace("&#39;211053&#39;,&#39;1113962&#39;",
                               "&#39;1113962&#39;,&#39;211053&#39;")
        assert parse_apply_now_options(f"<table>{bad}</table>") == []


class TestFindUnit:
    def test_od_prefix_is_stripped(self):
        """``find_unit`` 原来写死 ``xr_``，``od_211053`` 恒找不到。

        而「找不到」这条路径的表现是 race_lost（「已被他人选走」），所以这个
        bug 不会报错，只会让每一次 OurDomain 预订都静默失败。
        """
        opt = find_unit(UNITS_PAGE, "od_211053",
                        id_prefix="od_", parser=parse_apply_now_options)
        assert opt is not None and opt.unit_id == "211053"

    def test_wrong_parser_finds_nothing(self):
        """平台身份显式传，别做「自动识别」——配错解析器要能看出来。"""
        assert find_unit(UNITS_PAGE, "od_211053", id_prefix="od_") is None

    def test_a_different_unit_is_never_substituted(self):
        assert find_unit(UNITS_PAGE, "od_999999",
                         id_prefix="od_", parser=parse_apply_now_options) is None


# ── 平台钩子 ──────────────────────────────────────────────────────────


class TestPlatformHooks:
    def test_building_key_resolves_from_ourdomain_not_xior(self):
        b = OurDomainBooker()
        assert b._building_key(_listing()) == "diemen"
        assert b._building_key(_listing(city=SOUTH_EAST)) == "south-east"

    def test_unknown_city_returns_empty(self):
        assert OurDomainBooker()._building_key(_listing(city="Nowhere")) == ""

    def test_each_building_keeps_its_own_host(self):
        """两栋楼是两个 securerc 主机，cookie 不跨主机。

        串了就是拿这栋楼的凭据去登另一栋楼的门户——必然失败，还白烧一次
        RENTCafe 的 IP 尝试额度（连续失败锁 30 分钟）。
        """
        b = OurDomainBooker()
        diemen = b._floorplans_url(OurDomainScraper.BUILDINGS["diemen"])
        se = b._floorplans_url(OurDomainScraper.BUILDINGS["south-east"])
        assert diemen.startswith("https://thisisourdomain.securerc.co.uk/")
        assert se.startswith("https://southeast-thisisourdomain.securerc.co.uk/")
        assert "ourdomain-amsterdam-south-east" in se

    def test_id_prefix_matches_the_scraper(self):
        """前缀是单元匹配唯一的真相来源，两边必须一致。"""
        assert OurDomainBooker.id_prefix == OurDomainScraper.ID_PREFIX
        assert XiorBooker.id_prefix == "xr_"

    def test_account_comes_from_the_ourdomain_fields(self):
        ab = _user().auto_book
        assert OurDomainBooker()._account_for(ab, "diemen") == ("a@od.com", "pw")

    def test_xior_accounts_do_not_leak_into_ourdomain(self):
        ab = AutoBookConfig(xior_accounts={"diemen": {"email": "x@x", "password": "p"}})
        assert OurDomainBooker()._account_for(ab, "diemen") == ("", "")


# ── 入口段：走到 Applicant Info ────────────────────────────────────────


class _FakeSession:
    """按真实响应顺序回内容，记录每一步。"""

    instances: list = []

    def __init__(self, api_key, source="ourdomain"):
        self.calls: list[str] = []
        self.source = source
        self.opened = ""
        self.terms_payload = None
        self.terms_page_name = None
        self.units_html = UNITS_PAGE
        self.applicant_html = APPLICANT_PAGE
        self._html = ""
        self.saved_fields = None
        _FakeSession.instances.append(self)

    # --- URL 构造（真实实现按 open() 解析出的 base/ole_path 派生）---
    def ole_url(self, page):
        return ("https://thisisourdomain.securerc.co.uk/onlineleasing/"
                f"ourdomain-amsterdam-diemen/{page}")

    def content_url(self, query):
        return ("https://thisisourdomain.securerc.co.uk/onlineleasing/"
                f"rcLoadContent.ashx?{query}")

    def open(self, url):
        self.calls.append("open")
        self.opened = url
        self._html = FLOORPLANS_PAGE
        return {}

    def current_page_html(self):
        return self._html

    def fetch(self, url, *, referer="", ajax=False):
        self.calls.append(f"fetch:{'ajax' if ajax else 'plain'}")
        if "availableunits" in url:
            assert ajax, "rcLoadContent.ashx 缺 X-Requested-With 一律 403"
            return self.units_html
        return self.applicant_html

    def open_terms_for_unit(self, unit, *, referer=""):
        self.calls.append(f"book-now:{unit.unit_id}")
        self.terms_payload = unit
        self._html = TERMS_PAGE
        return TERMS_PAGE

    def submit_terms(self, fields, *, move_in_date="", page="oleapplication"):
        self.calls.append("submit_terms")
        self.terms_page_name = page
        self.terms_fields = dict(fields)
        return TERMS_PAGE

    def login(self, email, password, *, landed=None, landing_url=""):
        self.calls.append(f"login:{email}")
        self._html = self.applicant_html
        return {}

    def upload_document(self, html, filename, data):
        self.calls.append(f"upload:{filename}")
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


def _book(listing=None, user=None, session_cls=_FakeSession, doc=("p.pdf", b"x")):
    req = BookingRequest(listing=listing or _listing(), user=user or _user())
    with patch("bookers.rentcafe.RentCafeSession", session_cls), \
         patch("applicant_docs.load", lambda uid: doc):
        return OurDomainBooker().book(req)


class TestEntryFlow:
    def test_reaches_the_draft(self):
        r = _book()
        assert r.success is True and r.phase == "draft_saved"

    def test_walks_book_now_then_terms_then_login(self):
        _book()
        s = _FakeSession.instances[0]
        assert s.calls == [
            "open", "fetch:ajax", "book-now:211053", "submit_terms",
            "login:a@od.com", "upload:p.pdf", "fetch:plain", "save",
        ]

    def test_entry_is_the_building_url_not_listing_url(self):
        """listing.url 是 floorplans 展示链接；入口按楼栋元数据算。"""
        _book()
        s = _FakeSession.instances[0]
        assert s.opened.endswith("ourdomain-amsterdam-diemen/floorplans.aspx")

    def test_terms_step_uses_its_own_captcha_page(self):
        """OurDomain 的第 2 步是独立页 termsandotheritems.aspx。

        Referer 和验证码配置都得指向它，不能沿用 Xior 的 oleapplication。
        """
        _book()
        assert _FakeSession.instances[0].terms_page_name == "termsandotheritems"

    def test_terms_fields_come_from_the_page(self):
        """UnitId/FloorplanId 是服务端 POST 之后填进去的，必须原样带回。"""
        _book()
        f = _FakeSession.instances[0].terms_fields
        assert f["UnitId"] == "211053"
        assert f["FloorplanId"] == "1113962"
        assert f["QuotedRentEncr"].startswith("MTEzOC4wMA")

    def test_missing_unit_is_race_lost_not_an_error(self):
        """现查这张表顺带就是竞争检测：行没了 = 被人抢先，上层换备选房源。"""
        class _Gone(_FakeSession):
            def __init__(self, k, source="ourdomain"):
                super().__init__(k, source)
                self.units_html = "<html>Apartment Search Result</html>"

        r = _book(session_cls=_Gone)
        assert r.success is False and r.phase == "race_lost"
        assert "book-now" not in " ".join(_FakeSession.instances[0].calls)

    def test_relands_by_reselecting_the_unit_after_login(self):
        """登录会不会保住单元上下文，尚未端到端验证过（2026-08-04）。

        Xior 那边登录会重置到选房页，靠重新点一次单元救回来；OurDomain 没有
        选房页，对应动作就是再 POST 一次 Book now。这里模拟「登录后掉出了
        申请流程」，验证它会重选一次而不是直接放弃。
        """
        class _NeedsReselect(_FakeSession):
            def __init__(self, k, source="ourdomain"):
                super().__init__(k, source)
                self._logged_in = False

            def login(self, email, password, **kw):
                self.calls.append(f"login:{email}")
                self._logged_in = True
                self._html = "<html>back at the terms page</html>"
                return {}

            def open_terms_for_unit(self, unit, *, referer=""):
                out = super().open_terms_for_unit(unit, referer=referer)
                # 登录后重选：这一次才落到申请表
                return APPLICANT_PAGE if self._logged_in else out

            def submit_terms(self, fields, *, move_in_date="", page="oleapplication"):
                super().submit_terms(fields, move_in_date=move_in_date, page=page)
                return APPLICANT_PAGE if self._logged_in else TERMS_PAGE

        r = _book(session_cls=_NeedsReselect)
        s = _FakeSession.instances[0]
        assert s.calls.count("book-now:211053") == 2, "登录后没有重选单元"
        assert r.phase == "draft_saved"

    def test_stops_when_relanding_also_fails(self):
        """重选之后仍然不在申请表上，必须**明确停住**，不能盲填。

        猜一个 stepname 深链过去只会拿到空壳页（真正的内容走 rcLoadContent
        拉，缺 ProspectId 一律 500），而在空壳页上填表 = 在用户真实账号下提交
        一份空白申请，且毫无报错。
        """
        class _Lost(_FakeSession):
            """入口正常建起来了，只是登录之后再也回不到申请表。"""

            def login(self, email, password, **kw):
                self.calls.append(f"login:{email}")
                self._html = "<html>somewhere else</html>"
                return {}

            def submit_terms(self, fields, *, move_in_date="", page="oleapplication"):
                super().submit_terms(fields, move_in_date=move_in_date, page=page)
                return TERMS_PAGE

        r = _book(session_cls=_Lost)
        s = _FakeSession.instances[0]
        assert r.success is False
        assert r.phase != "draft_saved"
        assert "save" not in s.calls
        assert "upload:p.pdf" not in s.calls, "连证件都不该传上去"
        assert "尚未端到端" in r.message, (
            "失败原因要说清是这一段没验证过，而不是含糊的「页面结构无法识别」"
        )

    def test_pay_url_points_at_the_application_not_the_listing(self):
        """listing.url 是房源列表；用户要的是「回去把这单做完」。"""
        r = _book()
        assert r.pay_url.endswith("oleapplication.aspx")
        assert "floorplans" not in r.pay_url

    def test_message_never_claims_the_unit_is_secured(self):
        r = _book()
        assert "付款" in r.message
        for banned in ("已抢到", "预订成功", "已为你占"):
            assert banned not in r.message


class TestPreflight:
    def test_no_credentials_skips_without_network(self):
        r = _book(user=_user(email="", password=""))
        assert r.phase == "not_configured"
        assert _FakeSession.instances == []

    def test_unknown_city_is_unsupported(self):
        r = _book(listing=_listing(city="Nowhere"))
        assert r.phase == "unsupported"
        assert _FakeSession.instances == []

    def test_message_says_ourdomain_not_xior(self):
        """文案原来写死 Xior——用户会跑去核对一个他根本没配过的账号。"""
        r = _book(listing=_listing(city="Nowhere"))
        assert "OurDomain" in r.message and "Xior" not in r.message
