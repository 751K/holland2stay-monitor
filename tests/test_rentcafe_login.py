"""RENTCafe 密码登录测试。

``guestlogin.aspx`` 上有 4 个表单，最容易搞错的是拿错那个：

    Login       Username + Password + CheckUserAuth   ← 密码登录
    UserLogin   Username + reCAPTCHA + otpclicked…    ← OTP / 免密登录
    OtpOptions / VerifyOTP                            ← OTP 后续

**密码登录表单上没有任何 reCAPTCHA 字段**（实测 2026-08-03）。旧实现在这里
解了 v2+v3 又塞进不存在的字段，纯属浪费，还可能被当成异常流量。
"""
from __future__ import annotations

import pytest

from bookers.rentcafe import RentCafeSession


class _Resp:
    def __init__(self, text="", status=200, url="https://h.test/onlineleasing/p/x.aspx"):
        self.text = text
        self.status_code = status
        self.url = url


class _Session(RentCafeSession):
    """把网络层换成录音机，只看请求形状。"""

    def __init__(self, probe_html="", final_html=""):
        self._solver_calls = []
        self._base_url = "https://h.test"
        self._ole_path = "/onlineleasing/p"
        self._post_url = "https://h.test/onlineleasing/rcformsave.ashx"
        self._cafeportalkey = "CPK-1"
        self._impersonate = "chrome124"
        self.posts: list[dict] = []
        self._probe_html = probe_html
        self._final_html = final_html

        class _NoSolver:
            def __getattr__(inner, name):
                def _boom(*a, **k):
                    self._solver_calls.append(name)
                    raise AssertionError("密码登录不该解 reCAPTCHA")
                return _boom
        self._solver = _NoSolver()

    def _get(self, url):
        return _Resp('<input type="hidden" name="formName" value="F1">'
                     '<input type="hidden" name="myQS" value="Q1">')

    def _post(self, url, data, referer=""):
        self.posts.append(dict(data))
        return _Resp(self._probe_html if len(self.posts) == 1 else self._final_html)

    @staticmethod
    def _handle_response(resp, step):
        return {"_step": step}


class TestTwoRequestShape:
    def test_sends_probe_then_credentials(self):
        """页面上是渐进显示，网络层是两个 POST，靠 CheckUserAuth 区分。"""
        s = _Session()
        s.login("a@x.com", "pw")
        assert len(s.posts) == 2
        probe, real = s.posts
        assert probe["CheckUserAuth"] == "1"
        assert "Password" not in probe, "探测阶段不该带密码"
        assert real["CheckUserAuth"] == "0"
        assert real["Password"] == "pw"

    def test_both_carry_username_and_form_name(self):
        s = _Session()
        s.login("a@x.com", "pw")
        for p in s.posts:
            assert p["Username"] == "a@x.com"
            assert p["formName2"] == "Login", "必须是 Login 而不是 UserLogin"
            assert p["cafeportalkey"] == "CPK-1"

    def test_carries_server_hidden_fields(self):
        s = _Session()
        s.login("a@x.com", "pw")
        assert s.posts[0]["formName"] == "F1"
        assert s.posts[0]["myQS"] == "Q1"

    def test_probe_response_fields_are_merged(self):
        """服务端在探测后可能下发更新的隐藏字段，第二次要用新的。"""
        s = _Session(probe_html='<input type="hidden" name="SecurityCode" value="S2">')
        s.login("a@x.com", "pw")
        assert s.posts[1]["SecurityCode"] == "S2"
        assert s.posts[1]["Username"] == "a@x.com", "合并不能把 Username 冲掉"


class TestNoCaptcha:
    def test_password_login_never_solves_captcha(self):
        """密码登录表单上没有 reCAPTCHA 字段——解了纯属浪费，还可能被当异常流量。"""
        s = _Session()
        s.login("a@x.com", "pw")
        assert s._solver_calls == []

    def test_does_not_send_captcha_fields(self):
        s = _Session()
        s.login("a@x.com", "pw")
        for p in s.posts:
            assert "g-recaptcha-response-v3" not in p
            assert "failed-captcha-3" not in p


class TestFormChoice:
    def test_uses_the_password_form_not_the_otp_one(self):
        """UserLogin 那个表单是 OTP / 免密登录，拿错就永远登不上。"""
        assert RentCafeSession._LOGIN_FORM == "Login"

    def test_register_is_gone(self):
        """账号由用户自己注册；批量自动注册对平台是滥用。"""
        assert not hasattr(RentCafeSession, "register")
