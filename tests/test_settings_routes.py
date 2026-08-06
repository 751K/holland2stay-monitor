"""
app/routes/settings.py 路由测试。

覆盖：
- GET /settings 权限
- POST /settings CSRF 保护
- POST 写入 app_settings 表
- 非法值清洗（safety.sanitize_dotenv）

v1.16.0 起这些值住在 SQLite 的 app_settings，不再写 .env——.env 同时被人和程序
写，是 app/env_writer.py 那把锁与 write_env_key() 不能用 os.replace() 的根源。
本文件因此断言的是**表里的值**，不再是文件内容。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _saved(client_or_dir=None):
    """读回刚保存的配置。测试进程与路由共用同一个 DB_PATH（isolated_data_dir）。"""
    from config import DB_PATH
    from storage import Storage

    st = Storage(DB_PATH)
    try:
        return st.all_app_settings()
    finally:
        st.close()


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
        saved = _saved()
        assert saved["CHECK_INTERVAL"] == "120"

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
        saved = _saved()
        assert saved["PEAK_INTERVAL"] == "45"
        assert saved["MIN_INTERVAL"] == "10"
        assert saved["PEAK_START"] == "08:00"
        assert saved["PEAK_END"] == "09:30"
        assert saved["LOG_LEVEL"] == "DEBUG"
        # CITIES 应为 | 拼接格式
        assert saved["CITIES"] == "Eindhoven,29|Amsterdam,24"

    def test_save_sources_h2s_and_ourdomain(self, admin_client, isolated_data_dir):
        r = admin_client.post("/settings", data={
            "CHECK_INTERVAL": "300",
            "LOG_LEVEL": "INFO",
            "source_selected": ["holland2stay", "ourdomain"],
            "city_selected": ["Eindhoven,29"],
            "ourdomain_city_selected": ["Amsterdam Diemen,diemen"],
        }, headers={"X-CSRF-Token": "test_csrf"})
        assert r.status_code in (200, 302)
        saved = _saved()
        assert saved["SOURCES"] == "holland2stay,ourdomain"
        assert saved["CITIES"] == "Eindhoven,29"
        assert saved["OURDOMAIN_CITIES"] == "Amsterdam Diemen,diemen"

    def test_save_sources_ourdomain_only(self, admin_client, isolated_data_dir):
        r = admin_client.post("/settings", data={
            "CHECK_INTERVAL": "300",
            "LOG_LEVEL": "INFO",
            "source_selected": ["ourdomain"],
            "city_selected": ["Eindhoven,29"],
            "ourdomain_city_selected": ["Amsterdam Diemen,diemen"],
        }, headers={"X-CSRF-Token": "test_csrf"})
        assert r.status_code in (200, 302)
        saved = _saved()
        assert saved["SOURCES"] == "ourdomain"

    def test_empty_sources_falls_back_to_h2s(self, admin_client, isolated_data_dir):
        r = admin_client.post("/settings", data={
            "CHECK_INTERVAL": "300",
            "LOG_LEVEL": "INFO",
            "city_selected": ["Eindhoven,29"],
        }, headers={"X-CSRF-Token": "test_csrf"})
        assert r.status_code in (200, 302)
        saved = _saved()
        assert saved["SOURCES"] == "holland2stay"

    def test_invalid_numeric_not_written(self, admin_client, isolated_data_dir):
        """非法数字（abc）与空值都不写入，保留旧值。

        写进去的后果是 load_config() 里 int("abc") 直接抛，monitor 起不来——
        面板上一个手滑就能让监控停摆。
        """
        from config import DB_PATH
        from storage import Storage
        st = Storage(DB_PATH)
        st.set_app_settings({"PEAK_INTERVAL": "45", "CHECK_INTERVAL": "300"})
        st.close()
        r = admin_client.post("/settings", data={
            "CHECK_INTERVAL": "300",
            "PEAK_INTERVAL": "abc",
            "HEARTBEAT_INTERVAL_MINUTES": "",
            "LOG_LEVEL": "INFO",
            "city_selected": "Eindhoven,29",
        }, headers={"X-CSRF-Token": "test_csrf"})
        assert r.status_code in (200, 302)
        saved = _saved()
        assert saved["PEAK_INTERVAL"] == "45", "非法值覆盖了旧值"
        assert "HEARTBEAT_INTERVAL_MINUTES" not in saved, "空值写成了空字符串"


class TestStructuredValidation:
    """结构化配置坏了要挡在写入之前。

    这些值是分隔符拼出来的字符串，坏掉的后果不一致：CITIES 解析失败会静默变成
    0 个城市（监控照跑，什么都不抓），SHARD_SIZES 非法则让 load_config() 抛
    ValueError，monitor 起不来。挡在入口，坏值就进不了库。
    """

    def _post(self, client, **extra):
        data = {
            "CHECK_INTERVAL": "300", "LOG_LEVEL": "INFO",
            "source_selected": "holland2stay",
            "city_selected": "Eindhoven,29",
        }
        data.update(extra)
        return client.post("/settings", data=data,
                           headers={"X-CSRF-Token": "test_csrf"})

    def test_malformed_city_is_rejected(self, admin_client, isolated_data_dir):
        self._post(admin_client, city_selected="Eindhoven")
        assert "CITIES" not in _saved(), "格式错的城市写进库了"

    def test_nothing_else_is_saved_either(self, admin_client, isolated_data_dir):
        """整批一起拒绝：一半新一半旧比全旧更难排查。"""
        self._post(admin_client, city_selected="Eindhoven", CHECK_INTERVAL="123")
        assert _saved().get("CHECK_INTERVAL") != "123"

    def test_valid_config_still_saves(self, admin_client, isolated_data_dir):
        self._post(admin_client, city_selected=["Eindhoven,29", "Amsterdam,24"])
        assert _saved()["CITIES"] == "Eindhoven,29|Amsterdam,24"

    def test_unknown_city_id_saves_with_a_warning(self, admin_client, isolated_data_dir):
        """官方城市表会更新，不认识的 ID 不该阻止保存。"""
        self._post(admin_client, city_selected="SomeNewTown,99999")
        assert _saved()["CITIES"] == "SomeNewTown,99999"
