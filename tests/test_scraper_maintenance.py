"""
平台维护态测试。

需求：H2S 有时一直 403 是因为他们整站在维护（不是 Cloudflare 屏蔽）。
旧路径会把所有 403 当 Cloudflare WAF 屏蔽，发用户告警 + 走 15 min cooldown。
维护态下用户什么都做不了，告警是噪音；正确做法是连续 403 后探主站，
命中维护页就抛 UpstreamMaintenanceError 让 monitor 安静等待。

契约
----
1. 连续 N 次 403 时，_post_gql 会 GET 主站
2. 主站 body 含 "We'll be back soon" / "scheduled maintenance" → 抛 UpstreamMaintenanceError
3. 主站正常 → 仍然走 BlockedError 路径（旧行为）
4. dispatcher 优先把 UpstreamMaintenanceError 上抛（vs BlockedError）
5. is_maintenance_body 对正常 HTML / JSON 不误判
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import scraper
from scrapers.base import (
    UpstreamMaintenanceError,
    is_maintenance_body,
    probe_h2s_maintenance,
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


# ─── is_maintenance_body 单元测试 ─────────────────────────────────


class TestIsMaintenanceBody:
    def test_we_will_be_back_soon(self):
        assert is_maintenance_body("We'll be back soon")
        assert is_maintenance_body("We will be back soon")
        # case insensitive
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


# ─── probe_h2s_maintenance 单元测试 ───────────────────────────────


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
        """探测自己挂了不应该升级成更严重错误，返回 False 就好。"""
        session = MagicMock()
        session.get.side_effect = TimeoutError("probe timeout")
        assert probe_h2s_maintenance(session) is False


# ─── _post_gql 403 streak → maintenance 探测 ─────────────────────


class TestPostGqlMaintenanceProbe:
    def setup_method(self):
        # 跨测试不要把 streak 状态泄露过去
        scraper._consecutive_403_count = 0

    def teardown_method(self):
        scraper._consecutive_403_count = 0

    def test_single_403_does_not_probe(self):
        """阈值是 3，前 2 次 403 不应该触发探测。"""
        from scraper import _post_gql, BlockedError

        session = MagicMock()
        session.post.return_value = _fake_response(403, "Cloudflare challenge")

        with pytest.raises(BlockedError):
            _post_gql(session, "query{}")
        with pytest.raises(BlockedError):
            _post_gql(session, "query{}")

        # 仍然没有 GET 主站（streak=2 < 阈值 3）
        assert session.get.call_count == 0

    def test_consecutive_403_triggers_probe_and_raises_maintenance(self):
        """连续 3 次 403 + 主站维护 → UpstreamMaintenanceError，非 BlockedError。"""
        from scraper import _post_gql, BlockedError

        session = MagicMock()
        session.post.return_value = _fake_response(403, "Cloudflare challenge")
        # 主站显示维护页（探测会命中）
        session.get.return_value = _fake_response(503, _MAINT_HTML)

        # 前 2 次：BlockedError（不探测）
        with pytest.raises(BlockedError):
            _post_gql(session, "query{}")
        with pytest.raises(BlockedError):
            _post_gql(session, "query{}")
        assert session.get.call_count == 0

        # 第 3 次：跨过阈值，探测命中，抛 UpstreamMaintenanceError
        with pytest.raises(UpstreamMaintenanceError) as excinfo:
            _post_gql(session, "query{}")
        assert session.get.call_count == 1
        assert "维护" in str(excinfo.value) or "maintenance" in str(excinfo.value).lower()

    def test_consecutive_403_with_normal_main_site_still_blocked(self):
        """连续 3 次 403 但主站正常 → 走 BlockedError 路径（非维护）。"""
        from scraper import _post_gql, BlockedError

        session = MagicMock()
        session.post.return_value = _fake_response(403, "Cloudflare challenge")
        # 主站正常 → 不是维护
        session.get.return_value = _fake_response(200, "<html>OK</html>")

        with pytest.raises(BlockedError):
            _post_gql(session, "query{}")
        with pytest.raises(BlockedError):
            _post_gql(session, "query{}")
        with pytest.raises(BlockedError):
            _post_gql(session, "query{}")

        # 探测被触发过一次（streak 跨阈值时）
        assert session.get.call_count == 1

    def test_success_resets_streak(self):
        """成功响应后 streak 清零，下次 403 重新从 1 开始计数。"""
        from scraper import _post_gql, BlockedError

        session = MagicMock()
        # 403, 403, 403（应该探测）, 200, 403, 403 → 此 streak=2，不应该再探测
        ok = _fake_response(200, "")
        ok.json = MagicMock(return_value={"data": {"products": {"items": []}}})

        responses = [
            _fake_response(403, "cf"),
            _fake_response(403, "cf"),
            _fake_response(403, "cf"),
            ok,
            _fake_response(403, "cf"),
            _fake_response(403, "cf"),
        ]
        session.post.side_effect = responses
        # 主站探测：正常（不是维护）→ Block 仍抛
        session.get.return_value = _fake_response(200, "<html>OK</html>")

        # 1st, 2nd 403 - 不探
        with pytest.raises(BlockedError):
            _post_gql(session, "q1")
        with pytest.raises(BlockedError):
            _post_gql(session, "q2")
        assert session.get.call_count == 0
        # 3rd 403 - 跨阈值，探测
        with pytest.raises(BlockedError):
            _post_gql(session, "q3")
        assert session.get.call_count == 1
        # 成功响应
        _post_gql(session, "q4")
        assert scraper._consecutive_403_count == 0, "成功后 streak 必须清零"
        # 5th, 6th 403 - streak 重新计数，到 2 < 3 不再探测
        with pytest.raises(BlockedError):
            _post_gql(session, "q5")
        with pytest.raises(BlockedError):
            _post_gql(session, "q6")
        assert session.get.call_count == 1, "新一轮 streak 还没到阈值不应再探"


# ─── dispatcher 上抛优先级测试 ───────────────────────────────────


class TestDispatcherMaintenancePriority:
    """所有任务失败时，UpstreamMaintenanceError 应该比 BlockedError 优先上抛。"""

    def test_maintenance_wins_over_blocked(self):
        from scrapers import dispatch_scrape_tasks
        from scrapers.base import BlockedError, ScrapeTask

        # 1 个 H2S task：抛维护；1 个 OurDomain task：抛 Block
        tasks = [
            ScrapeTask(source="holland2stay", city_key="29", city_display="Eindhoven"),
            ScrapeTask(source="ourdomain", city_key="amsterdam", city_display="Amsterdam"),
        ]

        def fake_scrape(self, task):
            if task.source == "holland2stay":
                raise UpstreamMaintenanceError("h2s maintenance")
            raise BlockedError("ourdomain blocked")

        with patch("scrapers.holland2stay.HollandStayScraper.scrape", fake_scrape), \
             patch("scrapers.ourdomain.OurDomainScraper.scrape", fake_scrape):
            with pytest.raises(UpstreamMaintenanceError):
                dispatch_scrape_tasks(tasks)


# ─── monitor 维护态 → admin 通知 ─────────────────────────────────


class TestMonitorMaintenanceAdminNotify:
    """run_once 遇到维护时：不发普通用户告警，但给 admin web 通知面板发一条。"""

    def setup_method(self):
        import monitor
        monitor._last_maintenance_notify_at = 0.0
        monitor.prewarm_cache.clear()

    def teardown_method(self):
        import monitor
        monitor._last_maintenance_notify_at = 0.0
        monitor.prewarm_cache.clear()

    def _run(self, tmp_path, *, user_notify_capture, admin_notify_capture):
        """跑一次 run_once，分别捕获 user 渠道和 admin web 渠道收到的消息。"""
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
            # send_error 默认走 _send；不另外覆盖

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
        # 普通用户：零通知（凌晨维护不该吵醒人）
        assert user_msgs == [], f"用户通道不应该收到维护通知，实际收到 {user_msgs}"
        # admin：1 条
        assert len(admin_msgs) == 1, f"admin 应该收到 1 条，实际 {len(admin_msgs)}"
        assert "维护" in admin_msgs[0] or "maintenance" in admin_msgs[0].lower()

    def test_maintenance_notify_throttled_1h(self, tmp_path):
        user_msgs: list[str] = []
        admin_msgs: list[str] = []
        # 连续 3 次维护
        for _ in range(3):
            with pytest.raises(UpstreamMaintenanceError):
                self._run(
                    tmp_path,
                    user_notify_capture=user_msgs,
                    admin_notify_capture=admin_msgs,
                )
        # 1 小时窗口内应该只发 1 次
        assert len(admin_msgs) == 1, (
            f"1 小时内多次维护应该只发 1 条，实际 {len(admin_msgs)}"
        )

    def test_maintenance_notify_unthrottled_after_interval(self, tmp_path):
        import monitor
        user_msgs: list[str] = []
        admin_msgs: list[str] = []

        # 第一次
        with pytest.raises(UpstreamMaintenanceError):
            self._run(tmp_path, user_notify_capture=user_msgs, admin_notify_capture=admin_msgs)
        assert len(admin_msgs) == 1

        # 把节流戳往回拨 61 分钟
        monitor._last_maintenance_notify_at -= 61 * 60

        # 第二次：应该再发
        with pytest.raises(UpstreamMaintenanceError):
            self._run(tmp_path, user_notify_capture=user_msgs, admin_notify_capture=admin_msgs)
        assert len(admin_msgs) == 2

    def test_maintenance_writes_meta(self, tmp_path):
        from storage import Storage
        user_msgs: list[str] = []
        admin_msgs: list[str] = []
        with pytest.raises(UpstreamMaintenanceError):
            self._run(tmp_path, user_notify_capture=user_msgs, admin_notify_capture=admin_msgs)
        # 验证 meta 被写
        st = Storage(tmp_path / "test.db", timezone_str="UTC")
        try:
            assert st.get_meta("upstream_maintenance_seen_at", default="") != ""
            assert st.get_meta("upstream_maintenance_last_at", default="") != ""
        finally:
            st.close()
