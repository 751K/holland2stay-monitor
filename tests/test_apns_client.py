"""
ApnsClient 单元测试（httpx.MockTransport 注入，不开网络）
==========================================================

项目其它 async 测试都用 ``asyncio.run(go())`` 包装而非 pytest-asyncio，
本文件保持同风格。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tests.conftest_apns import apns_cfg, test_p8_key  # noqa: F401
from tests.conftest_fcm import fcm_cfg, test_rsa_key, test_service_account_json  # noqa: F401


def _run(coro):
    return asyncio.run(coro)


# ── ApnsConfig.from_env ────────────────────────────────────────────


class TestConfigFromEnv:
    def test_disabled_returns_none(self, monkeypatch):
        from notifier_channels.apns import ApnsConfig
        monkeypatch.delenv("APNS_ENABLED", raising=False)
        assert ApnsConfig.from_env() is None

    def test_missing_fields_returns_none(self, monkeypatch):
        from notifier_channels.apns import ApnsConfig
        monkeypatch.setenv("APNS_ENABLED", "true")
        monkeypatch.delenv("APNS_KEY_PATH", raising=False)
        assert ApnsConfig.from_env() is None

    def test_nonexistent_p8_returns_none(self, monkeypatch, tmp_path):
        from notifier_channels.apns import ApnsConfig
        monkeypatch.setenv("APNS_ENABLED", "true")
        monkeypatch.setenv("APNS_KEY_PATH", str(tmp_path / "missing.p8"))
        monkeypatch.setenv("APNS_KEY_ID", "A" * 10)
        monkeypatch.setenv("APNS_TEAM_ID", "B" * 10)
        monkeypatch.setenv("APNS_TOPIC", "com.x.y")
        assert ApnsConfig.from_env() is None

    def test_complete_returns_config(self, monkeypatch, test_p8_key):
        from notifier_channels.apns import ApnsConfig
        monkeypatch.setenv("APNS_ENABLED", "true")
        monkeypatch.setenv("APNS_KEY_PATH", str(test_p8_key))
        monkeypatch.setenv("APNS_KEY_ID", "A" * 10)
        monkeypatch.setenv("APNS_TEAM_ID", "B" * 10)
        monkeypatch.setenv("APNS_TOPIC", "com.x.y")
        cfg = ApnsConfig.from_env()
        assert cfg is not None and cfg.topic == "com.x.y"


# ── _host_for_env ──────────────────────────────────────────────────


class TestHost:
    def test_production(self):
        from notifier_channels.apns import _host_for_env
        assert _host_for_env("production") == "api.push.apple.com"

    def test_sandbox(self):
        from notifier_channels.apns import _host_for_env
        assert _host_for_env("sandbox") == "api.sandbox.push.apple.com"

    def test_unknown_defaults_to_production(self):
        from notifier_channels.apns import _host_for_env
        assert _host_for_env("weird") == "api.push.apple.com"


# ── 辅助 ───────────────────────────────────────────────────────────


def _ok_handler(request):
    return httpx.Response(200, headers={"apns-id": "test-apns-id-123"})


def _make_handler(status, reason):
    def h(request):
        if status == 200:
            return httpx.Response(200, headers={"apns-id": "ok"})
        return httpx.Response(
            status,
            content=json.dumps({"reason": reason}).encode(),
        )
    return h


def _client(apns_cfg, handler):
    from notifier_channels.apns import ApnsClient
    return ApnsClient(apns_cfg, transport=httpx.MockTransport(handler))


# ── send_one ───────────────────────────────────────────────────────


class TestSendOne:
    def test_200_ok(self, apns_cfg):
        async def go():
            client = _client(apns_cfg, _ok_handler)
            r = await client.send_one(
                device_token="a" * 64, env="production",
                payload={"aps": {"alert": "hi"}},
            )
            await client.close()
            return r
        r = _run(go())
        assert r.ok and r.status == 200
        assert r.apns_id == "test-apns-id-123"

    def test_410_unregistered_is_device_dead(self, apns_cfg):
        async def go():
            client = _client(apns_cfg, _make_handler(410, "Unregistered"))
            r = await client.send_one(
                device_token="b" * 64, env="production",
                payload={"aps": {}},
            )
            await client.close()
            return r
        r = _run(go())
        assert not r.ok and r.device_dead
        assert r.reason == "Unregistered"

    def test_400_bad_token_is_device_dead(self, apns_cfg):
        async def go():
            client = _client(apns_cfg, _make_handler(400, "BadDeviceToken"))
            r = await client.send_one(
                device_token="c" * 64, env="production",
                payload={"aps": {}},
            )
            await client.close()
            return r
        assert _run(go()).device_dead

    def test_429_not_device_dead(self, apns_cfg):
        async def go():
            client = _client(apns_cfg, _make_handler(429, "TooManyRequests"))
            r = await client.send_one(
                device_token="d" * 64, env="production",
                payload={"aps": {}},
            )
            await client.close()
            return r
        r = _run(go())
        assert not r.ok and not r.device_dead

    def test_403_expired_provider_triggers_retry(self, apns_cfg):
        calls: list[int] = []

        def handler(request):
            calls.append(1)
            if len(calls) == 1:
                return httpx.Response(
                    403,
                    content=json.dumps({"reason": "ExpiredProviderToken"}).encode(),
                )
            return httpx.Response(200, headers={"apns-id": "ok"})

        async def go():
            from notifier_channels.apns import ApnsClient
            client = ApnsClient(apns_cfg, transport=httpx.MockTransport(handler))
            r = await client.send_one(
                device_token="e" * 64, env="production",
                payload={"aps": {}},
            )
            await client.close()
            return r
        r = _run(go())
        assert r.ok
        assert len(calls) == 2  # 1 次 403 + 1 次重试 200

    def test_network_exception_returns_status_0(self, apns_cfg):
        def handler(request):
            raise httpx.ConnectError("boom")

        async def go():
            from notifier_channels.apns import ApnsClient
            client = ApnsClient(apns_cfg, transport=httpx.MockTransport(handler))
            r = await client.send_one(
                device_token="f" * 64, env="production",
                payload={"aps": {}},
            )
            await client.close()
            return r
        r = _run(go())
        assert r.status == 0
        assert not r.ok and not r.device_dead

    def test_headers_sent(self, apns_cfg):
        captured: dict = {}

        def handler(request):
            captured["headers"] = dict(request.headers)
            captured["url"] = str(request.url)
            return httpx.Response(200)

        async def go():
            from notifier_channels.apns import ApnsClient
            client = ApnsClient(apns_cfg, transport=httpx.MockTransport(handler))
            await client.send_one(
                device_token="g" * 64, env="sandbox",
                payload={"aps": {}}, collapse_id="round-123",
            )
            await client.close()
        _run(go())
        h = captured["headers"]
        assert h["apns-topic"] == apns_cfg.topic
        assert h["apns-push-type"] == "alert"
        assert h["apns-priority"] == "10"
        assert h["apns-collapse-id"] == "round-123"
        assert h["authorization"].startswith("bearer ")
        assert "sandbox" in captured["url"]


# ── send_many ──────────────────────────────────────────────────────


class TestSendMany:
    def test_concurrent(self, apns_cfg):
        seen: list[str] = []

        def handler(request):
            tok = str(request.url).rsplit("/", 1)[-1]
            seen.append(tok)
            return httpx.Response(200, headers={"apns-id": tok[:8]})

        async def go():
            from notifier_channels.apns import ApnsClient
            client = ApnsClient(apns_cfg, transport=httpx.MockTransport(handler))
            targets = [
                {"device_token": "a" * 64, "env": "production"},
                {"device_token": "b" * 64, "env": "production"},
                {"device_token": "c" * 64, "env": "production"},
            ]
            results = await client.send_many(
                targets, payload={"aps": {"alert": "x"}},
            )
            await client.close()
            return results
        rs = _run(go())
        assert len(rs) == 3
        assert all(r.ok for r in rs)
        assert set(seen) == {"a" * 64, "b" * 64, "c" * 64}


# ── JWT 缓存 ────────────────────────────────────────────────────────


class TestJwtCache:
    def test_token_cached_until_expiry(self, apns_cfg):
        from notifier_channels.apns import _JwtSigner
        s = _JwtSigner(apns_cfg.key_path, apns_cfg.key_id, apns_cfg.team_id)
        t1 = s.token(now=1000)
        t2 = s.token(now=1100)   # 100s 后，仍在缓存
        assert t1 == t2

    def test_token_rotates_after_window(self, apns_cfg):
        from notifier_channels.apns import _JwtSigner
        s = _JwtSigner(apns_cfg.key_path, apns_cfg.key_id, apns_cfg.team_id)
        t1 = s.token(now=1000)
        t2 = s.token(now=1000 + _JwtSigner.REFRESH_INTERVAL + 10)
        assert t1 != t2

    def test_force_refresh(self, apns_cfg):
        from notifier_channels.apns import _JwtSigner
        s = _JwtSigner(apns_cfg.key_path, apns_cfg.key_id, apns_cfg.team_id)
        t2 = s.force_refresh()
        # force_refresh 内部用 time.time()，可能在同一秒生成相同 token
        # 这里只断言 force_refresh 成功返回非空
        assert isinstance(t2, str) and len(t2) > 50


# ── FCM 客户端（原 test_fcm_client.py） ─────────────────────────────
#
# FcmClient 单元测试（httpx.MockTransport 注入，不开网络）。
# 保持与 APNs 同风格的 asyncio.run() 包装。

@pytest.fixture(autouse=True)
def _patch_token(monkeypatch):
    """FCM 测试不开网络做 OAuth2 token exchange。"""
    from notifier_channels.fcm import _AccessTokenCache
    monkeypatch.setattr(
        _AccessTokenCache, "_exchange",
        lambda self, assertion: "fake-access-token-for-test",
    )


class TestFcmConfigFromEnv:
    def test_disabled_returns_none(self, monkeypatch):
        from notifier_channels.fcm import FcmConfig
        monkeypatch.delenv("FCM_ENABLED", raising=False)
        assert FcmConfig.from_env() is None

    def test_service_account_json(self, monkeypatch, test_service_account_json):
        from notifier_channels.fcm import FcmConfig
        monkeypatch.setenv("FCM_ENABLED", "true")
        monkeypatch.setenv("FCM_SERVICE_ACCOUNT_PATH", str(test_service_account_json))
        cfg = FcmConfig.from_env()
        assert cfg is not None
        assert cfg.project_id == "test-fcm-project"
        assert cfg.client_email == "test@test-fcm-project.iam.gserviceaccount.com"

    def test_missing_json_returns_none(self, monkeypatch, tmp_path):
        from notifier_channels.fcm import FcmConfig
        monkeypatch.setenv("FCM_ENABLED", "true")
        monkeypatch.setenv("FCM_SERVICE_ACCOUNT_PATH", str(tmp_path / "nope.json"))
        assert FcmConfig.from_env() is None

    def test_env_vars_fallback(self, monkeypatch):
        from notifier_channels.fcm import FcmConfig
        monkeypatch.setenv("FCM_ENABLED", "true")
        monkeypatch.delenv("FCM_SERVICE_ACCOUNT_PATH", raising=False)
        monkeypatch.setenv("FCM_PROJECT_ID", "test-pid")
        monkeypatch.setenv("FCM_CLIENT_EMAIL", "test@example.com")
        monkeypatch.setenv("FCM_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----")
        cfg = FcmConfig.from_env()
        assert cfg is not None
        assert cfg.project_id == "test-pid"

    def test_missing_env_vars_returns_none(self, monkeypatch):
        from notifier_channels.fcm import FcmConfig
        monkeypatch.setenv("FCM_ENABLED", "true")
        monkeypatch.delenv("FCM_SERVICE_ACCOUNT_PATH", raising=False)
        monkeypatch.delenv("FCM_PROJECT_ID", raising=False)
        assert FcmConfig.from_env() is None


def _fcm_client(fcm_cfg, handler):
    from notifier_channels.fcm import FcmClient
    return FcmClient(fcm_cfg, transport=httpx.MockTransport(handler))


def _fcm_make_handler(status, error_code="", *, message=""):
    """按 **FCM v1 真实的错误形状** 造响应。

    真实响应长这样（注意 message 是给人看的散文，机器判据在 status /
    details[].errorCode 里）：

        {"error": {"code": 404, "status": "NOT_FOUND",
                   "message": "Requested entity was not found.",
                   "details": [{"@type": ".../FcmError",
                                "errorCode": "UNREGISTERED"}]}}

    这个 helper 原先只造 ``message``，于是 device_dead 那几条用例是拿
    ``message="unregistered"`` 去比的——**把 bug 钉成了期望行为**：真实响应里
    message 是散文，判定从来没命中过，失效 token 一直没被清理。
    """
    def h(request):
        if status == 200:
            return httpx.Response(200, content=b'{"name":"projects/p/messages/msg-123"}')
        err = {
            "code": status,
            "message": message or "Requested entity was not found.",
        }
        if error_code:
            err["status"] = "NOT_FOUND" if status == 404 else "INVALID_ARGUMENT"
            err["details"] = [{
                "@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError",
                "errorCode": error_code,
            }]
        return httpx.Response(status, content=json.dumps({"error": err}).encode())
    return h


def _token_handler(request):
    """Mock OAuth2 token endpoint。"""
    return httpx.Response(200, content=b'{"access_token":"fake-token-xxx","expires_in":3600}')


class TestFcmSendOne:
    def test_200_ok(self, fcm_cfg):
        async def go():
            client = _fcm_client(fcm_cfg, _fcm_make_handler(200))
            r = await client.send_one(
                device_token="a" * 64,
                payload={"message": {"data": {"title": "hi", "body": "test"}}},
            )
            await client.close()
            return r
        r = _run(go())
        assert r.ok and r.status == 200
        assert "msg-123" in r.message_id

    def test_400_unregistered_is_device_dead(self, fcm_cfg):
        async def go():
            client = _fcm_client(fcm_cfg, _fcm_make_handler(400, "INVALID_ARGUMENT"))
            r = await client.send_one(
                device_token="dead" + "0" * 60,
                payload={"message": {"data": {"title": "x"}}},
            )
            await client.close()
            return r
        r = _run(go())
        assert not r.ok and r.device_dead

    def test_404_not_found_is_device_dead(self, fcm_cfg):
        async def go():
            client = _fcm_client(fcm_cfg, _fcm_make_handler(404, "UNREGISTERED"))
            r = await client.send_one(
                device_token="gone" + "0" * 60,
                payload={"message": {"data": {"title": "x"}}},
            )
            await client.close()
            return r
        assert _run(go()).device_dead

    def test_429_not_device_dead(self, fcm_cfg):
        async def go():
            client = _fcm_client(fcm_cfg, _fcm_make_handler(429, "Quota exceeded"))
            r = await client.send_one(
                device_token="ok" * 20,
                payload={"message": {"data": {"title": "x"}}},
            )
            await client.close()
            return r
        r = _run(go())
        assert not r.ok and not r.device_dead

    def test_401_retries_with_new_token(self, fcm_cfg):
        calls: list[int] = []

        def handler(request):
            calls.append(1)
            if len(calls) == 1:
                return httpx.Response(
                    401,
                    content=json.dumps({
                        "error": {"code": 401, "message": "Unauthorized"},
                    }).encode(),
                )
            return httpx.Response(200, content=b'{"name":"ok"}')

        async def go():
            from notifier_channels.fcm import FcmClient
            client = FcmClient(fcm_cfg, transport=httpx.MockTransport(handler))
            r = await client.send_one(
                device_token="a" * 64,
                payload={"message": {"data": {"title": "x"}}},
            )
            await client.close()
            return r
        r = _run(go())
        assert r.ok
        assert len(calls) == 2

    def test_network_exception_returns_status_0(self, fcm_cfg):
        def handler(request):
            raise httpx.ConnectError("no network")

        async def go():
            client = _fcm_client(fcm_cfg, handler)
            r = await client.send_one(
                device_token="net" * 20,
                payload={"message": {"data": {"title": "x"}}},
            )
            await client.close()
            return r
        r = _run(go())
        assert r.status == 0
        assert not r.ok and not r.device_dead
        assert "client_error" in r.reason

    def test_headers_and_body_sent(self, fcm_cfg):
        captured: dict = {}

        def handler(request):
            captured["headers"] = dict(request.headers)
            captured["body"] = request.content
            return httpx.Response(200, content=b'{"name":"ok"}')

        async def go():
            client = _fcm_client(fcm_cfg, handler)
            await client.send_one(
                device_token="tok" * 20,
                payload={
                    "message": {
                        "data": {"title": "hello", "body": "world"},
                        "android": {"priority": "high"},
                    },
                },
                collapse_key="new-listing-123",
            )
            await client.close()
        _run(go())
        h = captured["headers"]
        assert h["authorization"].startswith("Bearer ")
        assert h["content-type"].startswith("application/json")
        body = json.loads(captured["body"])
        msg = body["message"]
        assert msg["token"] == "tok" * 20
        assert msg["data"]["title"] == "hello"
        assert msg["android"]["collapse_key"] == "new-listing-123"


class TestFcmSendMany:
    def test_concurrent(self, fcm_cfg):
        seen: list[str] = []

        def handler(request):
            body = json.loads(request.content)
            tok = body["message"]["token"]
            seen.append(tok)
            return httpx.Response(200, content=json.dumps({"name": tok[:8]}).encode())

        async def go():
            client = _fcm_client(fcm_cfg, handler)
            targets = [
                {"device_token": "a" * 32},
                {"device_token": "b" * 32},
                {"device_token": "c" * 32},
            ]
            results = await client.send_many(
                targets, payload={"message": {"data": {"title": "x"}}},
            )
            await client.close()
            return results
        rs = _run(go())
        assert len(rs) == 3
        assert all(r.ok for r in rs)
        assert set(seen) == {"a" * 32, "b" * 32, "c" * 32}


class TestAccessTokenCache:
    def test_token_cached(self, fcm_cfg):
        from notifier_channels.fcm import _AccessTokenCache
        cache = _AccessTokenCache(
            fcm_cfg.client_email, fcm_cfg.private_key, fcm_cfg.token_uri,
        )
        inner = cache
        inner._exchange = lambda assertion: "cached-token"  # noqa: SLF001
        t1 = inner.token(now=1000)
        assert t1 == "cached-token"
        t2 = inner.token(now=1100)
        assert t2 == "cached-token"

    def test_force_refresh(self, fcm_cfg):
        from notifier_channels.fcm import _AccessTokenCache
        cache = _AccessTokenCache(
            fcm_cfg.client_email, fcm_cfg.private_key, fcm_cfg.token_uri,
        )
        cache._exchange = lambda a: "fresh-token"  # noqa: SLF001
        t = cache.force_refresh()
        assert t == "fresh-token"


class TestFcmResult:
    def test_ok(self):
        from notifier_channels.fcm import FcmResult
        r = FcmResult(status=200, reason="OK", device="tok")
        assert r.ok and not r.device_dead

    def test_device_dead_unregistered(self):
        from notifier_channels.fcm import FcmResult
        r = FcmResult(status=404, reason="UNREGISTERED", device="tok")
        assert not r.ok and r.device_dead

    def test_device_dead_bad_token(self):
        from notifier_channels.fcm import FcmResult
        r = FcmResult(status=400, reason="INVALID_ARGUMENT", device="tok")
        assert r.device_dead

    def test_prose_message_is_not_a_verdict(self):
        """**这条是那个 bug 的形状。**

        真实响应里 error.message 是「The registration token is not a valid FCM
        registration token」这样的散文。拿它当判据，任何常量都不相等，于是失效
        token 永远清理不掉，每轮都对着墓碑重发一次。
        """
        from notifier_channels.fcm import FcmResult
        prose = "The registration token is not a valid FCM registration token"
        assert not FcmResult(status=404, reason=prose, device="t").device_dead

    def test_reason_comes_from_error_code_not_message(self):
        """_build_result 必须取 details[].errorCode，其次 status，最后才是 message。"""
        from notifier_channels.fcm import FcmClient

        class _Resp:
            status_code = 404
            text = ""

            @staticmethod
            def json():
                return {"error": {
                    "code": 404, "status": "NOT_FOUND",
                    "message": "Requested entity was not found.",
                    "details": [{
                        "@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError",
                        "errorCode": "UNREGISTERED"}],
                }}

        r = FcmClient._build_result(None, _Resp(), "tok")
        assert r.reason == "UNREGISTERED", f"取错了字段：{r.reason!r}"
        assert r.device_dead
