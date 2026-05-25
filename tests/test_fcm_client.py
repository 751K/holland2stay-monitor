"""
FcmClient 单元测试（httpx.MockTransport 注入，不开网络）
==========================================================

保持与 test_apns_client.py 同风格的 asyncio.run() 包装。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tests.conftest_fcm import fcm_cfg, test_rsa_key, test_service_account_json  # noqa: F401


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _patch_token(monkeypatch):
    """所有本文件测试不开网络做 OAuth2 token exchange。"""
    from notifier_channels.fcm import _AccessTokenCache
    monkeypatch.setattr(
        _AccessTokenCache, "_exchange",
        lambda self, assertion: "fake-access-token-for-test",
    )


# ── FcmConfig.from_env ──────────────────────────────────────────────


class TestConfigFromEnv:
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


# ── 辅助 ─────────────────────────────────────────────────────────────


def _client(fcm_cfg, handler):
    from notifier_channels.fcm import FcmClient
    return FcmClient(fcm_cfg, transport=httpx.MockTransport(handler))


def _make_handler(status, error_message=""):
    def h(request):
        if status == 200:
            return httpx.Response(200, content=b'{"name":"projects/p/messages/msg-123"}')
        content = json.dumps({
            "error": {
                "code": status,
                "message": error_message or "Unknown error",
            },
        }).encode()
        return httpx.Response(status, content=content)
    return h


def _token_handler(request):
    """Mock OAuth2 token endpoint。"""
    return httpx.Response(200, content=b'{"access_token":"fake-token-xxx","expires_in":3600}')


# ── send_one ─────────────────────────────────────────────────────────


class TestSendOne:
    def test_200_ok(self, fcm_cfg):
        async def go():
            client = _client(fcm_cfg, _make_handler(200))
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
            client = _client(fcm_cfg, _make_handler(400, "registration-token-not-registered"))
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
            client = _client(fcm_cfg, _make_handler(404, "unregistered"))
            r = await client.send_one(
                device_token="gone" + "0" * 60,
                payload={"message": {"data": {"title": "x"}}},
            )
            await client.close()
            return r
        assert _run(go()).device_dead

    def test_429_not_device_dead(self, fcm_cfg):
        async def go():
            client = _client(fcm_cfg, _make_handler(429, "Quota exceeded"))
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
            client = _client(fcm_cfg, handler)
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
            client = _client(fcm_cfg, handler)
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


# ── send_many ────────────────────────────────────────────────────────


class TestSendMany:
    def test_concurrent(self, fcm_cfg):
        seen: list[str] = []

        def handler(request):
            body = json.loads(request.content)
            tok = body["message"]["token"]
            seen.append(tok)
            return httpx.Response(200, content=json.dumps({"name": tok[:8]}).encode())

        async def go():
            client = _client(fcm_cfg, handler)
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


# ── Token 缓存 ──────────────────────────────────────────────────────


class TestAccessTokenCache:
    def test_token_cached(self, fcm_cfg):
        from notifier_channels.fcm import _AccessTokenCache
        cache = _AccessTokenCache(
            fcm_cfg.client_email, fcm_cfg.private_key, fcm_cfg.token_uri,
        )
        # Use MockTransport for the token exchange
        import httpx
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


# ── FcmResult ────────────────────────────────────────────────────────


class TestFcmResult:
    def test_ok(self):
        from notifier_channels.fcm import FcmResult
        r = FcmResult(status=200, reason="OK", device="tok")
        assert r.ok and not r.device_dead

    def test_device_dead_unregistered(self):
        from notifier_channels.fcm import FcmResult
        r = FcmResult(status=404, reason="unregistered", device="tok")
        assert not r.ok and r.device_dead

    def test_device_dead_bad_token(self):
        from notifier_channels.fcm import FcmResult
        r = FcmResult(status=400, reason="invalid-argument", device="tok")
        assert r.device_dead
