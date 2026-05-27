"""
Resend Inbound email.received webhook 测试。

覆盖：
- Svix 签名生成 + 验证（正样本 / 错 secret / 错 timestamp / 缺 header）
- 时间窗（在窗口内 / 超过 5 min）
- 多签名轮换（v1,xxx v1,yyy 任一匹配通过）
- ``email.received`` 路径：写入 web_notifications + 反查 Resend API
- DMARC 报告识别 → 单独 notif type
- 错误 payload / 错事件类型不崩
- 未配 secret 时拒绝（不裸奔）
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from unittest.mock import patch

import pytest

from app.routes.inbound import (
    _decode_secret,
    _is_dmarc_report,
    _verify_svix,
)


# ─── _decode_secret ─────────────────────────────────────────────


class TestDecodeSecret:
    def test_strip_whsec_prefix(self):
        # b"hello" → base64 → "aGVsbG8="；加上 whsec_ 前缀
        raw = b"hello"
        s = "whsec_" + base64.b64encode(raw).decode()
        assert _decode_secret(s) == raw

    def test_without_prefix_treats_as_base64(self):
        raw = b"hello"
        assert _decode_secret(base64.b64encode(raw).decode()) == raw

    def test_invalid_base64_returns_none(self):
        assert _decode_secret("whsec_!!!not-base64!!!") is None

    def test_empty_returns_none(self):
        assert _decode_secret("") is None


# ─── _verify_svix ───────────────────────────────────────────────


def _make_sig(secret: str, svix_id: str, svix_ts: str, body: bytes) -> str:
    """模拟 Svix 服务端：用 secret 给 (id.ts.body) 签 HMAC-SHA256。"""
    secret_bytes = _decode_secret(secret)
    assert secret_bytes is not None, "test secret should decode"
    signed = f"{svix_id}.{svix_ts}.".encode() + body
    digest = hmac.new(secret_bytes, signed, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


_TEST_SECRET = "whsec_" + base64.b64encode(b"super-secret-test-key-1234567890").decode()


class TestVerifySvix:
    def test_valid_signature_passes(self):
        body = b'{"type":"email.received","data":{"email_id":"abc"}}'
        svix_id = "msg_1"
        svix_ts = str(int(time.time()))
        sig = _make_sig(_TEST_SECRET, svix_id, svix_ts, body)

        assert _verify_svix(body, {
            "svix-id": svix_id,
            "svix-timestamp": svix_ts,
            "svix-signature": sig,
        }, _TEST_SECRET) is True

    def test_wrong_secret_fails(self):
        body = b'{"x":1}'
        svix_id, svix_ts = "msg_1", str(int(time.time()))
        sig = _make_sig(_TEST_SECRET, svix_id, svix_ts, body)
        other_secret = "whsec_" + base64.b64encode(b"different-secret").decode()

        assert _verify_svix(body, {
            "svix-id": svix_id,
            "svix-timestamp": svix_ts,
            "svix-signature": sig,
        }, other_secret) is False

    def test_tampered_body_fails(self):
        body = b'{"x":1}'
        svix_id, svix_ts = "msg_1", str(int(time.time()))
        sig = _make_sig(_TEST_SECRET, svix_id, svix_ts, body)

        # 篡改 body
        assert _verify_svix(b'{"x":2}', {
            "svix-id": svix_id,
            "svix-timestamp": svix_ts,
            "svix-signature": sig,
        }, _TEST_SECRET) is False

    def test_old_timestamp_fails(self):
        """超过 ±5 min 容忍窗口 → 防重放，拒绝。"""
        body = b'{"x":1}'
        svix_id = "msg_1"
        svix_ts = str(int(time.time()) - 10 * 60)  # 10 分钟前
        sig = _make_sig(_TEST_SECRET, svix_id, svix_ts, body)

        assert _verify_svix(body, {
            "svix-id": svix_id,
            "svix-timestamp": svix_ts,
            "svix-signature": sig,
        }, _TEST_SECRET) is False

    def test_future_timestamp_fails(self):
        body = b'{"x":1}'
        svix_id = "msg_1"
        svix_ts = str(int(time.time()) + 10 * 60)  # 10 分钟后
        sig = _make_sig(_TEST_SECRET, svix_id, svix_ts, body)

        assert _verify_svix(body, {
            "svix-id": svix_id,
            "svix-timestamp": svix_ts,
            "svix-signature": sig,
        }, _TEST_SECRET) is False

    def test_missing_headers_fails(self):
        assert _verify_svix(b"{}", {}, _TEST_SECRET) is False
        assert _verify_svix(b"{}", {"svix-id": "x"}, _TEST_SECRET) is False
        assert _verify_svix(b"{}", {
            "svix-id": "x", "svix-timestamp": str(int(time.time())),
        }, _TEST_SECRET) is False

    def test_non_numeric_timestamp_fails(self):
        sig = _make_sig(_TEST_SECRET, "id", "1234", b"{}")
        assert _verify_svix(b"{}", {
            "svix-id": "id",
            "svix-timestamp": "not-a-number",
            "svix-signature": sig,
        }, _TEST_SECRET) is False

    def test_rotation_multiple_signatures(self):
        """svix-signature 可以是 'v1,xxx v1,yyy'——任一匹配即通过。"""
        body = b'{"x":1}'
        svix_id, svix_ts = "msg_1", str(int(time.time()))
        valid_sig = _make_sig(_TEST_SECRET, svix_id, svix_ts, body)
        # 拼接：一个无效的在前，有效的在后
        combined = "v1,bogus_sig_xxx " + valid_sig

        assert _verify_svix(body, {
            "svix-id": svix_id,
            "svix-timestamp": svix_ts,
            "svix-signature": combined,
        }, _TEST_SECRET) is True

    def test_unknown_signature_version_ignored(self):
        """v2,xxx 等未知版本号不应匹配——只认 v1。"""
        body = b'{"x":1}'
        svix_id, svix_ts = "msg_1", str(int(time.time()))
        sig = _make_sig(_TEST_SECRET, svix_id, svix_ts, body)
        # 把 v1, 改成 v2,
        bad = "v2," + sig[3:]

        assert _verify_svix(body, {
            "svix-id": svix_id,
            "svix-timestamp": svix_ts,
            "svix-signature": bad,
        }, _TEST_SECRET) is False


# ─── _is_dmarc_report ───────────────────────────────────────────


class TestIsDmarcReport:
    @pytest.mark.parametrize("sender,subject,expected", [
        ("noreply-dmarc-support@google.com", "Report Domain: flatradar.app",
         True),
        ("dmarc-noreply@yahoo.com", "DMARC Aggregate Report",
         True),
        ("postmaster@example.com", "Report-ID: 12345",
         True),
        ("user@gmail.com", "Hello there",
         False),
        ("support@flatradar.app", "Reply about my account",
         False),
    ])
    def test_classification(self, sender, subject, expected):
        assert _is_dmarc_report(sender, subject) is expected


# ─── 端到端：Flask 路由 ────────────────────────────────────────


@pytest.fixture
def flask_client(tmp_path, monkeypatch):
    """启动一个最小的 Flask app 注册 inbound 路由 + 隔离 DB。

    DB_PATH 是 config.py 在 import 时定的常量；改 env 没用。直接 patch
    inbound 模块里引入的 ``storage`` 工厂函数，让它指向临时 DB。
    """
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("RESEND_WEBHOOK_SECRET", _TEST_SECRET)
    monkeypatch.setenv("RESEND_API_KEY", "")

    from storage import Storage as RealStorage

    def _make_test_storage():
        return RealStorage(db_path, timezone_str="UTC")

    monkeypatch.setattr("app.routes.inbound.storage", _make_test_storage)

    from flask import Flask
    from app.routes import inbound

    app = Flask(__name__)
    app.config["TESTING"] = True
    inbound.register(app)
    return app.test_client(), db_path


def _post(client, body: dict, *, secret=_TEST_SECRET, svix_id="msg_1",
          ts: int | None = None) -> "FlaskResponse":
    raw = json.dumps(body, separators=(",", ":")).encode()
    svix_ts = str(ts if ts is not None else int(time.time()))
    sig = _make_sig(secret, svix_id, svix_ts, raw)
    return client.post(
        "/api/inbound/email",
        data=raw,
        content_type="application/json",
        headers={
            "Svix-Id": svix_id,
            "Svix-Timestamp": svix_ts,
            "Svix-Signature": sig,
        },
    )


def _email_received_payload(**overrides) -> dict:
    base = {
        "type": "email.received",
        "created_at": "2026-02-22T23:41:12.126Z",
        "data": {
            "email_id": "56761188-7520-42d8-8898-ff6fc54ce618",
            "from": "user@example.com",
            "to": ["notify@flatradar.app"],
            "subject": "Test inbound",
            "attachments": [],
        },
    }
    if "data" in overrides:
        base["data"].update(overrides.pop("data"))
    base.update(overrides)
    return base


class TestInboundRoute:
    def test_missing_secret_env_returns_503(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FLATRADAR_DB", str(tmp_path / "x.db"))
        monkeypatch.delenv("RESEND_WEBHOOK_SECRET", raising=False)

        from flask import Flask
        from app.routes import inbound

        app = Flask(__name__)
        inbound.register(app)
        client = app.test_client()

        r = client.post("/api/inbound/email", data=b"{}",
                        content_type="application/json")
        assert r.status_code == 503

    def test_invalid_signature_returns_401(self, flask_client):
        client, _ = flask_client
        raw = json.dumps(_email_received_payload()).encode()
        r = client.post(
            "/api/inbound/email",
            data=raw,
            content_type="application/json",
            headers={
                "Svix-Id": "msg_1",
                "Svix-Timestamp": str(int(time.time())),
                "Svix-Signature": "v1,obviously-wrong",
            },
        )
        assert r.status_code == 401

    def test_email_received_writes_notification(self, flask_client):
        client, db_path = flask_client
        # 不让它真去网络拉
        with patch("app.routes.inbound._fetch_full_email", return_value={
            "text": "Hello FlatRadar admin, just testing.",
            "html": "<p>Hello</p>",
        }):
            r = _post(client, _email_received_payload())

        assert r.status_code == 200, r.data
        body = r.get_json()
        assert body["ok"] is True
        assert body["email_id"] == "56761188-7520-42d8-8898-ff6fc54ce618"

        # 应该写了一条 web_notifications，type=inbound_email
        from storage import Storage
        st = Storage(db_path, timezone_str="UTC")
        try:
            notifs = st.get_notifications(limit=5)
            assert len(notifs) == 1
            assert notifs[0]["type"] == "inbound_email"
            assert "user@example.com" in notifs[0]["body"]
            assert "Hello FlatRadar" in notifs[0]["body"]
        finally:
            st.close()

    def test_dmarc_report_uses_dedicated_type(self, flask_client):
        client, db_path = flask_client
        with patch("app.routes.inbound._fetch_full_email", return_value=None):
            r = _post(client, _email_received_payload(data={
                "from": "noreply-dmarc-support@google.com",
                "subject": "Report Domain: flatradar.app",
            }))
        assert r.status_code == 200

        from storage import Storage
        st = Storage(db_path, timezone_str="UTC")
        try:
            notifs = st.get_notifications(limit=5)
            assert notifs[0]["type"] == "inbound_dmarc"
            assert "DMARC" in notifs[0]["title"]
        finally:
            st.close()

    def test_unknown_event_type_is_skipped(self, flask_client):
        client, _ = flask_client
        with patch("app.routes.inbound._fetch_full_email") as fetch:
            r = _post(client, {
                "type": "email.delivered",
                "data": {"email_id": "x"},
            })
        assert r.status_code == 200
        body = r.get_json()
        assert body.get("skipped") == "email.delivered"
        # 不应该尝试拉正文
        fetch.assert_not_called()

    def test_fetch_failure_does_not_break_webhook(self, flask_client):
        """反查 API 挂了不能让 Resend 重投——returns 200 with skeleton notification."""
        client, db_path = flask_client
        with patch("app.routes.inbound._fetch_full_email", return_value=None):
            r = _post(client, _email_received_payload())
        assert r.status_code == 200

        from storage import Storage
        st = Storage(db_path, timezone_str="UTC")
        try:
            assert len(st.get_notifications(limit=5)) == 1
        finally:
            st.close()
