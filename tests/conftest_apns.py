"""APNs 测试共享 fixture：生成测试用 .p8、构造 mock client。"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def test_p8_key(tmp_path_factory) -> Path:
    """生成一份合法的 ES256 .p8 密钥供 ApnsClient 加载测试。"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    priv = ec.generate_private_key(ec.SECP256R1())
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    p = tmp_path_factory.mktemp("apns") / "AuthKey_TEST.p8"
    p.write_bytes(pem)
    return p


@pytest.fixture
def apns_cfg(test_p8_key):
    from notifier_channels.apns import ApnsConfig
    return ApnsConfig(
        key_path=str(test_p8_key),
        key_id="TESTKID000",
        team_id="TESTTID000",
        topic="com.kong.h2smonitor",
        env_default="production",
        concurrency=4,
    )
