"""
平台维护态测试（适配新 CloakBrowser 路径）。

旧 curl_cffi _post_gql 的 403 streak / maintenance probe 逻辑已退役。
is_maintenance_body / probe_h2s_maintenance 仍保留在 scrapers.base，
但仅作为未来可能的探测工具。dispatcher 优先级 + monitor admin 通知逻辑不变。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from scrapers.base import (
    UpstreamMaintenanceError,
    is_maintenance_body,
    probe_h2s_maintenance,
    BlockedError,
    ScrapeTask,
)


_MAINT_HTML = """<!DOCTYPE html>
<html><body>
<h1>We'll be back soon</h1>
<p>We are currently performing scheduled maintenance to update our systems.
We anticipate being back online by 11:30(CET).</p>
</body></html>"""


def _fake_response(status_code, body=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = body
    resp.ok = 200 <= status_code < 300
    resp.json = MagicMock(return_value={"data": {}})
    def raise_for_status():
        if not resp.ok:
            raise Exception(f"HTTP Error {status_code}")
    resp.raise_for_status = raise_for_status
    return resp


# ─── is_maintenance_body 单元测试（不变）─────────────────────────

class TestIsMaintenanceBody:
    def test_we_will_be_back_soon(self):
        assert is_maintenance_body("We'll be back soon")
        assert is_maintenance_body("We will be back soon")
        assert is_maintenance_body("WE'LL BE BACK SOON")

    def test_scheduled_maintenance(self):
        assert is_maintenance_body("Currently performing scheduled maintenance")

    def test_full_h2s_maintenance_page(self):
        assert is_maintenance_body(_MAINT_HTML)

    def test_empty_returns_false(self):
        assert is_maintenance_body("") is False
        assert is_maintenance_body(None) is False  # type: ignore[arg-type]

    def test_normal_html_returns_false(self):
        normal = "<html><body><h1>Holland2Stay</h1><p>Find your home</p></body></html>"
        assert is_maintenance_body(normal) is False

    def test_graphql_json_returns_false(self):
        assert is_maintenance_body('{"data":{"products":{"items":[]}}}') is False


# ─── probe_h2s_maintenance 单元测试（不变）───────────────────────

class TestProbeH2sMaintenance:
    def test_probe_returns_true_when_main_site_in_maintenance(self):
        session = MagicMock()
        session.get.return_value = _fake_response(503, _MAINT_HTML)
        assert probe_h2s_maintenance(session) is True

    def test_probe_returns_false_for_normal_site(self):
        session = MagicMock()
        session.get.return_value = _fake_response(200, "<html><body>Welcome</body></html>")
        assert probe_h2s_maintenance(session) is False

    def test_probe_swallows_network_errors(self):
        session = MagicMock()
        session.get.side_effect = TimeoutError("probe timeout")
        assert probe_h2s_maintenance(session) is False


# ─── dispatcher 上抛优先级测试（不变）───────────────────────────

class TestDispatcherMaintenancePriority:
    def test_maintenance_wins_over_blocked(self):
        from contextlib import contextmanager
        from scrapers import dispatch_scrape_tasks

        tasks = [
            ScrapeTask(source="holland2stay", city_key="29", city_display="Eindhoven"),
            ScrapeTask(source="ourdomain", city_key="amsterdam", city_display="Amsterdam"),
        ]

        def fake_scrape(self, task):
            if task.source == "holland2stay":
                raise UpstreamMaintenanceError("h2s maintenance")
            raise BlockedError("ourdomain blocked")

        @contextmanager
        def _noop_batch(self):
            yield

        with patch("scrapers.holland2stay.HollandStayScraper.scrape", fake_scrape), \
             patch("scrapers.holland2stay.HollandStayScraper.batch_session", _noop_batch), \
             patch("scrapers.ourdomain.OurDomainScraper.scrape", fake_scrape):
            with pytest.raises(UpstreamMaintenanceError):
                dispatch_scrape_tasks(tasks)


# ─── monitor 维护态 → admin 通知（不变）─────────────────────────

class TestMonitorMaintenanceAdminNotify:
    def setup_method(self):
        import monitor
        monitor._last_maintenance_notify_at = 0.0
        monitor.prewarm_cache.clear()

    def teardown_method(self):
        import monitor
        monitor._last_maintenance_notify_at = 0.0
        monitor.prewarm_cache.clear()

    def _run(self, tmp_path, *, user_notify_capture, admin_notify_capture):
        import asyncio
        from monitor import run_once
        from notifier import BaseNotifier
        from users import UserConfig
        from config import AutoBookConfig, Config, CityFilter, AvailabilityFilter
        from storage import Storage

        cfg = Config(
            check_interval=300,
            cities=[CityFilter(name="E", id=29)],
            availability_filters=[AvailabilityFilter(label="A", id=179)],
            db_path=tmp_path / "test.db", log_level="WARNING",
        )

        class CapUserNotifier(BaseNotifier):
            has_channels = True
            async def _send(self, t):
                user_notify_capture.append(t)
                return True
            async def close(self): pass

        class CapAdminNotifier(BaseNotifier):
            has_channels = True
            async def _send(self, t): return True
            async def send_error(self, message):
                admin_notify_capture.append(message)
                return True
            async def close(self): pass

        user = UserConfig(
            name="A", id="aaaa", enabled=True, notifications_enabled=True,
            notification_channels=[],
            auto_book=AutoBookConfig(enabled=False),
        )
        notifs = [(user, CapUserNotifier())]
        admin = CapAdminNotifier()
        storage = Storage(tmp_path / "test.db", timezone_str="UTC")

        async def go():
            with patch(
                "monitor.dispatch_scrape_tasks",
                side_effect=UpstreamMaintenanceError("test maintenance"),
            ):
                await run_once(cfg, storage, notifs, web_notifier=admin, dry_run=False)

        try:
            asyncio.run(go())
        finally:
            storage.close()

    def test_first_maintenance_notifies_admin_only(self, tmp_path):
        user_msgs: list[str] = []
        admin_msgs: list[str] = []
        with pytest.raises(UpstreamMaintenanceError):
            self._run(
                tmp_path,
                user_notify_capture=user_msgs,
                admin_notify_capture=admin_msgs,
            )
        assert user_msgs == [], f"用户通道不应该收到维护通知，实际收到 {user_msgs}"
        assert len(admin_msgs) == 1, f"admin 应该收到 1 条，实际 {len(admin_msgs)}"
        assert "维护" in admin_msgs[0] or "maintenance" in admin_msgs[0].lower()

    def test_maintenance_notify_throttled_1h(self, tmp_path):
        user_msgs: list[str] = []
        admin_msgs: list[str] = []
        for _ in range(3):
            with pytest.raises(UpstreamMaintenanceError):
                self._run(
                    tmp_path,
                    user_notify_capture=user_msgs,
                    admin_notify_capture=admin_msgs,
                )
        assert len(admin_msgs) == 1, (
            f"1 小时内多次维护应该只发 1 条，实际 {len(admin_msgs)}"
        )

    def test_maintenance_notify_unthrottled_after_interval(self, tmp_path):
        import monitor
        user_msgs: list[str] = []
        admin_msgs: list[str] = []

        with pytest.raises(UpstreamMaintenanceError):
            self._run(tmp_path, user_notify_capture=user_msgs, admin_notify_capture=admin_msgs)
        assert len(admin_msgs) == 1

        monitor._last_maintenance_notify_at -= 61 * 60

        with pytest.raises(UpstreamMaintenanceError):
            self._run(tmp_path, user_notify_capture=user_msgs, admin_notify_capture=admin_msgs)
        assert len(admin_msgs) == 2

    def test_maintenance_writes_meta(self, tmp_path):
        from storage import Storage
        user_msgs: list[str] = []
        admin_msgs: list[str] = []
        with pytest.raises(UpstreamMaintenanceError):
            self._run(tmp_path, user_notify_capture=user_msgs, admin_notify_capture=admin_msgs)
        st = Storage(tmp_path / "test.db", timezone_str="UTC")
        try:
            assert st.get_meta("upstream_maintenance_seen_at", default="") != ""
            assert st.get_meta("upstream_maintenance_last_at", default="") != ""
        finally:
            st.close()
