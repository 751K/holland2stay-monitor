"""
邮箱验证链接安全测试。

核心约束：验证 URL 只能来自显式配置的 PUBLIC_BASE_URL，不能从请求 Host
fallback 生成，避免 Host Header 注入导致 token 被钓鱼域名截获。
"""
from __future__ import annotations

import pytest

from app.email_verify import _build_verify_url


class TestBuildVerifyUrl:
    def test_missing_public_base_url_fails_closed(self, monkeypatch):
        monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
        with pytest.raises(RuntimeError, match="PUBLIC_BASE_URL"):
            _build_verify_url("tok123")

    def test_http_public_base_url_rejected(self, monkeypatch):
        monkeypatch.setenv("PUBLIC_BASE_URL", "http://flatradar.app")
        with pytest.raises(RuntimeError, match="PUBLIC_BASE_URL"):
            _build_verify_url("tok123")

    def test_https_public_base_url_used(self, monkeypatch):
        monkeypatch.setenv("PUBLIC_BASE_URL", "https://flatradar.app/")
        assert _build_verify_url("tok123") == "https://flatradar.app/verify-email/tok123"
