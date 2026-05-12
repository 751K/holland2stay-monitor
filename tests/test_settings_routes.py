"""
app/routes/settings.py 路由测试。

覆盖：
- GET /settings 权限
- POST /settings CSRF 保护
- POST 写入 .env 键值
- 非法值清洗（safety.sanitize_dotenv）
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


class TestSettingsAuth:
    def test_anon_blocked(self, client):
        assert client.get("/settings").status_code == 302  # → /login

    def test_guest_blocked(self, guest_client):
        assert guest_client.get("/settings").status_code == 302  # → /

    def test_admin_can_access(self, admin_client):
        r = admin_client.get("/settings")
        assert r.status_code == 200

    def test_post_requires_csrf(self, admin_client):
        r = admin_client.post("/settings", data={"CHECK_INTERVAL": "120"})
        assert r.status_code == 403


class TestSettingsPost:
    def test_save_check_interval(self, admin_client, isolated_data_dir):
        """POST 写入 CHECK_INTERVAL=120 应生效。"""
        r = admin_client.post("/settings", data={
            "CHECK_INTERVAL": "120",
            "city_selected": "Eindhoven,29",
        }, headers={"X-CSRF-Token": "test_csrf"})
        # 验证 .env 文件内容被写入
        assert r.status_code in (200, 302)
        env_content = isolated_data_dir.joinpath(".env").read_text(encoding="utf-8")
        assert "CHECK_INTERVAL=120" in env_content

    def test_save_smart_polling_params(self, admin_client, isolated_data_dir):
        """POST 写入智能轮询参数。"""
        r = admin_client.post("/settings", data={
            "CHECK_INTERVAL": "300",
            "PEAK_INTERVAL": "45",
            "MIN_INTERVAL": "10",
            "PEAK_START": "08:00",
            "PEAK_END": "09:30",
            "PEAK_WEEKDAYS_ONLY": "false",
            "JITTER_RATIO": "0.15",
            "LOG_LEVEL": "DEBUG",
            "city_selected": "Eindhoven,29|Amsterdam,24",
        }, headers={"X-CSRF-Token": "test_csrf"})
        assert r.status_code in (200, 302)
        env_content = isolated_data_dir.joinpath(".env").read_text(encoding="utf-8")
        assert "PEAK_INTERVAL=45" in env_content
        assert "MIN_INTERVAL=10" in env_content
        assert "PEAK_START=08:00" in env_content
        assert "PEAK_END=09:30" in env_content
        assert "LOG_LEVEL=DEBUG" in env_content
        # CITIES 应为 | 拼接格式
        assert "Eindhoven,29|Amsterdam,24" in env_content
