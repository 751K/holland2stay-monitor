"""NextAuth 登录流的单测。

2026-08-19 H2S 把登录从 GraphQL ``generateCustomerToken`` 换成 NextAuth
credentials 三步握手（docs/H2S_BOOKING_OPS.md §6）。这个文件钉住那三步、
钉住凭据错误 / 2FA / 明文传输这几条分支。
"""
from __future__ import annotations

import json

import pytest

from booker import AuthError, TwoFactorRequiredError, login


class _FakeFetcher:
    """按脚本回应 fetch_plain 的假 fetcher。

    responses: {path: {"status":..., "text":...}}；记录每次调用供断言。
    """

    def __init__(self, responses):
        self._responses = responses
        self.calls = []

    def fetch_plain(self, path, *, method="GET", body="", headers=None,
                    timeout_ms=30_000):
        self.calls.append({"path": path, "method": method, "body": body,
                           "headers": headers or {}})
        r = self._responses[path]
        return {"status": r.get("status", 200), "ok": True,
                "text": r["text"], "headers": {}}


def _ok_responses(access_token="JWT.abc.def", requires2fa=False):
    return {
        "/api/auth/csrf": {"text": json.dumps({"csrfToken": "csrf-xyz"})},
        "/api/auth/callback/credentials": {"status": 200,
                                           "text": json.dumps({"url": "https://www.holland2stay.com/"})},
        "/api/auth/session": {"text": json.dumps({
            "user": {}, "accessToken": access_token,
            "requires2fa": requires2fa, "twoFaPending": requires2fa,
        })},
    }


class TestHappyPath:
    def test_returns_access_token(self):
        f = _FakeFetcher(_ok_responses(access_token="the-jwt"))
        assert login(f, "u@x.com", "pw") == "the-jwt"

    def test_three_step_handshake_in_order(self):
        f = _FakeFetcher(_ok_responses())
        login(f, "u@x.com", "pw")
        assert [c["path"] for c in f.calls] == [
            "/api/auth/csrf",
            "/api/auth/callback/credentials",
            "/api/auth/session",
        ]

    def test_callback_is_form_urlencoded_and_carries_csrf(self):
        f = _FakeFetcher(_ok_responses())
        login(f, "u@x.com", "pw")
        cb = f.calls[1]
        assert cb["method"] == "POST"
        assert cb["headers"].get("Content-Type") == "application/x-www-form-urlencoded"
        # csrf 必须回传，email 必须带上，redirect=false 才回 JSON
        assert "csrfToken=csrf-xyz" in cb["body"]
        assert "email=u%40x.com" in cb["body"]
        assert "redirect=false" in cb["body"]

    def test_password_is_sent_only_in_callback_body(self):
        """密码只出现在 callback 那一步，不出现在 csrf / session。"""
        f = _FakeFetcher(_ok_responses())
        login(f, "u@x.com", "s3cret")
        assert "s3cret" in f.calls[1]["body"]
        assert "s3cret" not in f.calls[0]["body"]
        assert "s3cret" not in (f.calls[2]["body"] or "")


class TestCredentialFailure:
    def test_callback_401_raises_auth_error(self):
        r = _ok_responses()
        r["/api/auth/callback/credentials"] = {"status": 401, "text": ""}
        with pytest.raises(AuthError):
            login(_FakeFetcher(r), "u@x.com", "wrong")

    def test_callback_json_error_raises_auth_error(self):
        r = _ok_responses()
        r["/api/auth/callback/credentials"] = {
            "status": 200, "text": json.dumps({"error": "CredentialsSignin"})}
        with pytest.raises(AuthError):
            login(_FakeFetcher(r), "u@x.com", "wrong")

    def test_empty_session_raises_auth_error(self):
        r = _ok_responses()
        r["/api/auth/session"] = {"text": json.dumps({})}
        with pytest.raises(AuthError):
            login(_FakeFetcher(r), "u@x.com", "wrong")


class TestTwoFactor:
    def test_session_requires2fa_without_token_raises_2fa(self):
        r = _ok_responses(access_token=None, requires2fa=True)
        r["/api/auth/session"] = {"text": json.dumps(
            {"user": {}, "requires2fa": True})}
        with pytest.raises(TwoFactorRequiredError):
            login(_FakeFetcher(r), "u@x.com", "pw")

    def test_token_but_still_pending_raises_2fa(self):
        """即便 token 已发，仍标记待验证也按 2FA 处理——拿未完成校验的 token
        去下单会在后续步骤莫名失败。"""
        r = _ok_responses(access_token="partial", requires2fa=True)
        with pytest.raises(TwoFactorRequiredError):
            login(_FakeFetcher(r), "u@x.com", "pw")

    def test_2fa_is_not_auth_error(self):
        """两者必须分开：提示不同，且 2FA 不该被当成凭据错误。"""
        assert not issubclass(TwoFactorRequiredError, AuthError)
        assert not issubclass(AuthError, TwoFactorRequiredError)


class TestMalformed:
    def test_missing_csrf_raises_runtime(self):
        r = _ok_responses()
        r["/api/auth/csrf"] = {"text": json.dumps({})}
        with pytest.raises(RuntimeError):
            login(_FakeFetcher(r), "u@x.com", "pw")

    def test_csrf_non_json_raises_runtime(self):
        r = _ok_responses()
        r["/api/auth/csrf"] = {"text": "<!DOCTYPE html> just a moment"}
        with pytest.raises(RuntimeError):
            login(_FakeFetcher(r), "u@x.com", "pw")


class TestTransport:
    def test_login_uses_fetch_plain_not_fetch_gql(self):
        """登录不能走 fetch_gql——那条会被套上加密信封，NextAuth 端点收到会 400。

        用一个只有 fetch_plain、故意没有 fetch_gql 的 fetcher：能登录成功
        就证明没碰 fetch_gql。"""
        class _OnlyPlain:
            def __init__(self, responses):
                self._r = responses
            def fetch_plain(self, path, **kw):
                r = self._r[path]
                return {"status": r.get("status", 200), "ok": True,
                        "text": r["text"], "headers": {}}
            def fetch_gql(self, *a, **k):
                raise AssertionError("登录不该调用 fetch_gql（会被加密信封 400）")
        assert login(_OnlyPlain(_ok_responses(access_token="t")), "u@x.com", "pw") == "t"
