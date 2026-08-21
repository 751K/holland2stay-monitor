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


class TestUncheckingAllCitiesSticks:
    """取消勾选全部楼盘之后，保存要真的生效。

    2026-08-21 用户报的：把 OURDOMAIN 下唯一的「Amsterdam Diemen」取消勾选、
    保存，刷新回来又勾上了。原因是空选时回落到硬编码默认值：

        od_cities_val = "|".join(selected) if selected else "Amsterdam Diemen,diemen"

    界面把**没保存的东西显示成保存了**——这比拒绝保存还糟，用户会以为是自己
    点错了。CITIES 有同样的写法，XIOR_CITIES 一直是允许空的。
    """

    def _post(self, client, **extra):
        data = {
            "CHECK_INTERVAL": "300", "LOG_LEVEL": "INFO",
            "source_selected": ["holland2stay", "ourdomain"],
            "city_selected": "Eindhoven,29",
            "ourdomain_city_selected": "Amsterdam Diemen,diemen",
        }
        data.update(extra)
        return client.post("/settings", data=data,
                           headers={"X-CSRF-Token": "test_csrf"})

    def test_unchecking_ourdomain_cities_saves_empty(self, admin_client, isolated_data_dir):
        self._post(admin_client)
        assert _saved()["OURDOMAIN_CITIES"] == "Amsterdam Diemen,diemen"

        self._post(admin_client, ourdomain_city_selected=[])
        assert _saved()["OURDOMAIN_CITIES"] == "", "取消勾选又被写回默认城市了"

    def test_unchecking_h2s_cities_saves_empty(self, admin_client, isolated_data_dir):
        self._post(admin_client, city_selected=[])
        assert _saved()["CITIES"] == ""

    def test_the_settings_page_renders_them_unchecked(self, admin_client, isolated_data_dir):
        """存进去还不够——GET 回来必须真的是没勾上的状态。"""
        self._post(admin_client, ourdomain_city_selected=[])
        html = admin_client.get("/settings").get_data(as_text=True)
        for line in html.splitlines():
            if 'name="ourdomain_city_selected"' in line:
                assert "checked" not in line, "保存成空了，页面却还勾着"

    def test_empty_list_means_no_tasks(self, isolated_data_dir, monkeypatch):
        """空列表在 config 层确实等于「这个平台不抓」，不是回落到全部。"""
        monkeypatch.setenv("SOURCES", "ourdomain")
        monkeypatch.setenv("OURDOMAIN_CITIES", "")
        import config
        cfg = config.load_config()
        assert cfg.ourdomain_cities == []
        assert [t for t in cfg.scrape_tasks_v2() if t.source == "ourdomain"] == []

    def test_warns_when_an_enabled_source_has_no_target(self, admin_client, isolated_data_dir):
        """合法但十有八九不是本意——只提示，不代劳改配置。"""
        r = self._post(admin_client, ourdomain_city_selected=[])
        html = admin_client.get("/settings").get_data(as_text=True)
        assert "OurDomain" in html and "不会抓取" in html

    def test_no_warning_when_the_source_is_disabled(self, admin_client, isolated_data_dir):
        """平台本来就没启用，没勾楼盘是理所当然的，不该报。"""
        self._post(admin_client, source_selected="holland2stay",
                   ourdomain_city_selected=[])
        html = admin_client.get("/settings").get_data(as_text=True)
        assert "不会抓取" not in html


class TestXiorEmptyMeansAll:
    """XIOR_CITIES 的「空」和另外两个**不是一个意思**，别一视同仁。

    config.py:1844 的既有约定：XIOR_CITIES 留空 = 全部 30 栋楼。所以对 xior
    报「已启用但不会抓取」是错的。
    """

    def test_empty_xior_still_yields_all_buildings(self, isolated_data_dir, monkeypatch):
        monkeypatch.setenv("SOURCES", "xior")
        monkeypatch.setenv("XIOR_CITIES", "")
        import config
        cfg = config.load_config()
        assert len(cfg.xior_cities) == len(config.KNOWN_XIOR_CITIES) > 1

    def test_empty_xior_does_not_warn(self, admin_client, isolated_data_dir):
        admin_client.post("/settings", data={
            "CHECK_INTERVAL": "300", "LOG_LEVEL": "INFO",
            "source_selected": ["holland2stay", "xior"],
            "city_selected": "Eindhoven,29",
            "xior_city_selected": [],
        }, headers={"X-CSRF-Token": "test_csrf"})
        html = admin_client.get("/settings").get_data(as_text=True)
        assert "不会抓取" not in html
