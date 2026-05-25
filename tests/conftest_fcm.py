"""FCM 测试共享 fixture：生成测试 RSA 密钥、service account JSON、mock client。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def test_rsa_key(tmp_path_factory) -> Path:
    """生成 RSA 2048 PEM 私钥供 FcmClient OAuth2 签名测试。"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    priv = rsa.generate_private_key(65537, 2048)
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    p = tmp_path_factory.mktemp("fcm") / "test-key.pem"
    p.write_bytes(pem)
    return p


@pytest.fixture(scope="session")
def test_service_account_json(tmp_path_factory, test_rsa_key) -> Path:
    """生成一份有效的 service account JSON 供 FcmConfig.from_env 加载。"""
    key_text = test_rsa_key.read_text()
    sa = {
        "type": "service_account",
        "project_id": "test-fcm-project",
        "private_key_id": "abc123",
        "private_key": key_text,
        "client_email": "test@test-fcm-project.iam.gserviceaccount.com",
        "client_id": "12345",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    p = tmp_path_factory.mktemp("fcm") / "service-account.json"
    p.write_text(json.dumps(sa))
    return p


@pytest.fixture
def fcm_cfg(test_service_account_json):
    from notifier_channels.fcm import FcmConfig
    return FcmConfig._from_service_account(str(test_service_account_json))


@pytest.fixture
def mock_token_exchange(monkeypatch):
    """替换 OAuth2 token exchange，避免测试时开网络。"""
    from notifier_channels.fcm import _AccessTokenCache
    monkeypatch.setattr(
        _AccessTokenCache, "_exchange",
        lambda self, assertion: "fake-access-token-for-test",
    )
