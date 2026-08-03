"""HAR 清洗测试。

侦察 RENTCafe 登录之后的流程要靠管理员手动走一遍并导出 HAR，而 **HAR 会原样
记录密码和会话 cookie**——登录那个 POST 的 body 里就是明文密码，之后每个请求
都带着能直接冒充本人的 session cookie。所以 HAR 不能原样交出去。

这里守两个方向，缺一不可：
1. 该抹的必须抹干净（密码 / cookie / 令牌 / 账号标识）
2. **该留的必须留住**——抹过头会让 HAR 失去侦察价值，尤其是
   ``MoveInDateEncr`` 和 ``failed-captcha-3-rentable`` 这类关键字段。
"""
from __future__ import annotations

import json

import pytest

from tools.sanitize_har import REDACTED, _is_sensitive, sanitize, summarize


def _har(entries):
    return {"log": {"version": "1.2", "entries": entries}}


def _entry(url="https://a-xiorstudenthousing.securerc.co.uk/onlineleasing/p/rcformsave.ashx",
           params=None, text="", req_headers=None, res_headers=None, body="<html>ok</html>"):
    return {
        "request": {
            "method": "POST", "url": url,
            "headers": req_headers or [],
            "queryString": [],
            "cookies": [{"name": "sess", "value": "SECRET"}],
            "postData": {"params": params or [], "text": text},
        },
        "response": {
            "status": 200,
            "headers": res_headers or [],
            "cookies": [{"name": "sess", "value": "SECRET"}],
            "content": {"text": body},
        },
    }


class TestFieldClassification:
    @pytest.mark.parametrize("name", [
        "Password", "password", "xior_password", "AuthToken",
        "Username", "UserEmail", "g-recaptcha-response-v3", "otpVerification1",
    ])
    def test_sensitive(self, name):
        assert _is_sensitive(name) is True

    @pytest.mark.parametrize("name", [
        # 这些是侦察真正要看的东西，抹掉就白导了
        "failed-captcha-3-rentable", "MoveInDateEncr", "QuotedRentEncr",
        "formName2", "cafeportalkey", "sMoveInDate", "FloorplanId", "UnitTypeId",
    ])
    def test_not_sensitive(self, name):
        assert _is_sensitive(name) is False

    def test_captcha_flag_survives_recaptcha_rule(self):
        """规则写成 'recaptcha-response' 而不是 'captcha'，否则会连
        failed-captcha-3-rentable 一起误伤，而它的 true/false 是关键信号。"""
        assert _is_sensitive("g-recaptcha-response-v3") is True
        assert _is_sensitive("failed-captcha-3-rentable") is False


class TestRedaction:
    def test_strips_password_in_params_and_text(self):
        e = _entry(
            params=[{"name": "Username", "value": "me@x.com"},
                    {"name": "Password", "value": "hunter2"},
                    {"name": "formName2", "value": "UserLogin"}],
            text="Username=me%40x.com&Password=hunter2&formName2=UserLogin",
        )
        out = sanitize(_har([e]))
        raw = json.dumps(out)
        assert "hunter2" not in raw
        assert "me@x.com" not in raw and "me%40x.com" not in raw
        # 结构保住了
        assert "formName2" in raw and "UserLogin" in raw

    def test_strips_cookies_everywhere(self):
        e = _entry(
            req_headers=[{"name": "Cookie", "value": "ASP.NET_SessionId=SECRET"}],
            res_headers=[{"name": "Set-Cookie", "value": "sess=SECRET"}],
        )
        out = sanitize(_har([e]))
        raw = json.dumps(out)
        assert "SECRET" not in raw
        assert out["log"]["entries"][0]["request"]["cookies"] == []
        assert out["log"]["entries"][0]["response"]["cookies"] == []

    def test_strips_long_tokens_from_bodies(self):
        e = _entry(body="<html>" + "Z" * 300 + "</html>")
        out = sanitize(_har([e]))
        assert "Z" * 300 not in json.dumps(out)

    def test_json_body_is_redacted_recursively(self):
        e = _entry(text=json.dumps({"user": {"password": "hunter2", "id": 7}}))
        out = sanitize(_har([e]))
        assert "hunter2" not in json.dumps(out)
        assert '"id": 7' in out["log"]["entries"][0]["request"]["postData"]["text"]


class TestPreservation:
    def test_keeps_signed_fields_needed_for_recon(self):
        """MoveInDateEncr 是当前最可能卡死自动化的机制，必须留住。"""
        e = _entry(params=[
            {"name": "MoveInDateEncr", "value": "My04LTIwMjY=-z8g85jGXmr8="},
            {"name": "failed-captcha-3-rentable", "value": "false"},
        ])
        out = sanitize(_har([e]))
        vals = {p["name"]: p["value"]
                for p in out["log"]["entries"][0]["request"]["postData"]["params"]}
        assert vals["MoveInDateEncr"] == "My04LTIwMjY=-z8g85jGXmr8="
        assert vals["failed-captcha-3-rentable"] == "false"

    def test_sensitive_field_names_are_kept(self):
        """只抹值不抹名——要能看出「这一步提交了密码字段」。"""
        e = _entry(params=[{"name": "Password", "value": "x"}])
        p = sanitize(_har([e]))["log"]["entries"][0]["request"]["postData"]["params"]
        assert p[0]["name"] == "Password" and p[0]["value"] == REDACTED

    def test_does_not_mutate_input(self):
        e = _entry(params=[{"name": "Password", "value": "hunter2"}])
        har = _har([e])
        sanitize(har)
        assert har["log"]["entries"][0]["request"]["postData"]["params"][0]["value"] == "hunter2"


class TestFiltering:
    def test_host_filter_drops_third_party(self):
        keep = _entry()
        drop = _entry(url="https://www.google-analytics.com/collect")
        out = sanitize(_har([keep, drop]), host_filter="securerc.co.uk")
        assert len(out["log"]["entries"]) == 1

    def test_no_bodies_drops_response_text(self):
        out = sanitize(_har([_entry()]), keep_bodies=False)
        assert "text" not in out["log"]["entries"][0]["response"]["content"]

    def test_empty_har(self):
        assert sanitize(_har([]))["log"]["entries"] == []


class TestSummary:
    def test_lists_steps_with_field_names(self):
        e = _entry(params=[{"name": "formName2", "value": "termsandotheritems"}])
        s = summarize(sanitize(_har([e])))
        assert "POST" in s and "rcformsave.ashx" in s and "formName2" in s
