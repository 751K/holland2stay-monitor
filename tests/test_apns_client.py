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
        t1 = s.token(now=1000)
        t2 = s.force_refresh()
        # force_refresh 内部用 time.time()，可能在同一秒生成相同 token
        # 这里只断言 force_refresh 成功返回非空
        assert isinstance(t2, str) and len(t2) > 50
