"""RENTCafe 各页 reCAPTCHA 配置表测试。

这张表存在的理由是一个实测推翻的假设：原来代码假设「RENTCafe 全线用同一套
Enterprise reCAPTCHA」，于是 solve_v3() 写死 enterprise=1 并默认用
`6LfBeqEa…`。实测条款页用的是**标准 v3** 和另一个 sitekey——按旧假设去解，
token 服务端不认。

所以这里守的不是「值等于多少」（那些会随上游变），而是**「各页确实不一样」
这个结构性事实**不被后人重新抹平。
"""
from __future__ import annotations

import pytest

from captcha import (
    KIND_NONE,
    KIND_V3,
    KIND_V3_ENTERPRISE,
    RENTCAFE_PAGES,
    RENTCAFE_V2_SITEKEY,
    page_captcha,
)


class TestLookup:
    def test_accepts_bare_name_and_aspx(self):
        assert page_captcha("guestlogin").action == "UserLogin"
        assert page_captcha("guestlogin.aspx").action == "UserLogin"

    def test_is_case_and_space_insensitive(self):
        assert page_captcha("  REGISTER.aspx ").action == "GuestRegistration"

    def test_unknown_page_returns_none_not_a_default(self):
        """未侦察过的页必须返回 None。

        给个「默认配置」兜底等于把已经踩过的坑重新埋回去——调用方会拿着
        register 的 sitekey 去解 Applicant Info 页，然后拿到一个无效 token。
        """
        assert page_captcha("applicantinfo") is None
        assert page_captcha("leasesummary") is None


class TestPagesActuallyDiffer:
    """核心契约：不是全站一套。"""

    def test_terms_page_is_not_enterprise(self):
        p = page_captcha("oleapplication")
        assert p.kind == KIND_V3
        assert p.is_enterprise is False

    def test_login_and_register_are_enterprise(self):
        for name in ("guestlogin", "register"):
            assert page_captcha(name).kind == KIND_V3_ENTERPRISE
            assert page_captcha(name).is_enterprise is True

    def test_terms_sitekey_differs_from_login(self):
        assert page_captcha("oleapplication").v3_sitekey != page_captcha("guestlogin").v3_sitekey

    def test_every_captcha_page_has_a_distinct_action(self):
        acts = [p.action for p in RENTCAFE_PAGES.values() if p.has_captcha]
        assert len(acts) == len(set(acts)), "action 必须逐页不同，Google 按 action 计分"

    def test_fallback_flag_field_differs(self):
        assert page_captcha("oleapplication").fallback_flag == "failed-captcha-3-rentable"
        assert page_captcha("guestlogin").fallback_flag == "failed-captcha-3"


class TestSharedBits:
    def test_v2_sitekey_is_shared(self):
        for p in RENTCAFE_PAGES.values():
            if p.has_captcha:
                assert p.v2_sitekey == RENTCAFE_V2_SITEKEY

    def test_v3_token_field_is_shared(self):
        for p in RENTCAFE_PAGES.values():
            if p.has_captcha:
                assert p.v3_field == "g-recaptcha-response-v3"


class TestFlexRegistrationIsNotABypass:
    """它没有验证码，但不是旁路——两个出口都回到带验证码的 register.aspx。"""

    def test_has_no_captcha(self):
        p = page_captcha("flexregistrationlandingpage")
        assert p.kind == KIND_NONE
        assert p.has_captcha is False
        assert p.v3_sitekey == ""

    def test_captcha_pages_still_required(self):
        """真正要过的是 register/guestlogin，它们仍然带 Enterprise 验证码。"""
        assert page_captcha("register").has_captcha
        assert page_captcha("guestlogin").has_captcha


class TestSolverHonoursEnterpriseFlag:
    def test_solve_v3_passes_enterprise_flag_through(self):
        """回归测试：enterprise 曾经被写死为 1。"""
        from captcha.solver import CaptchaSolver

        captured = {}

        class _FakeClient:
            def recaptcha(self, **kw):
                captured.update(kw)
                return {"code": "tok"}

        solver = CaptchaSolver.__new__(CaptchaSolver)   # 跳过 __init__ 的 SDK 导入
        solver._api_key = "x"
        solver._client = _FakeClient()

        p = page_captcha("oleapplication")
        solver.solve_v3(
            page_url="https://x.test/oleapplication.aspx",
            action=p.action, sitekey=p.v3_sitekey, enterprise=p.is_enterprise,
        )
        assert captured["enterprise"] == 0, "标准 v3 页不能按 Enterprise 求解"
        assert captured["sitekey"] == p.v3_sitekey
        assert captured["action"] == "start_application"

        solver.solve_v3(
            page_url="https://x.test/guestlogin.aspx",
            action="UserLogin", sitekey="k", enterprise=True,
        )
        assert captured["enterprise"] == 1


class TestV2FieldIsDistinct:
    """v2 token 不能塞进 v3 字段。

    2026-08-03 实测踩过：塞错字段后服务端一直回「Please verify that you are
    not a robot」，既不报字段错误也不报 token 无效，只能靠对着页面 JS 才看
    得出来——v2 由 grecaptcha.render() 渲染，token 落在标准的
    g-recaptcha-response 上。
    """

    def test_v2_and_v3_fields_differ(self):
        for name in ("oleapplication", "guestlogin", "register"):
            p = page_captcha(name)
            assert p.v2_field != p.v3_field, f"{name} 的 v2/v3 字段不该相同"

    def test_v2_field_is_the_standard_one(self):
        assert page_captcha("oleapplication").v2_field == "g-recaptcha-response"
