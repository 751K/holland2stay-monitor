"""
平台维护态测试（适配新 CloakBrowser 路径）。

旧 curl_cffi ``_post_gql`` 的 403 streak / maintenance probe 逻辑已退役，
``probe_h2s_maintenance`` 也随之删除（浏览器导航本来就经过主站，顺手判掉即可）。

现在的判定路径：``browser_fetcher._h2s_maintenance_check`` 作为 H2S_PROFILE 的
``maintenance_check`` 钩子，在 CF 挑战解开**之后**拿页面标题/正文调
``is_maintenance_body``。dispatcher 优先级 + monitor admin 通知逻辑不变。
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from scrapers.base import (
    UpstreamMaintenanceError,
    is_maintenance_body,
    BlockedError,
    ScrapeTask,
)


_MAINT_HTML = """<!DOCTYPE html>
<html><body>
<h1>We'll be back soon</h1>
<p>We are currently performing scheduled maintenance to update our systems.
We anticipate being back online by 11:30(CET).</p>
</body></html>"""




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


class TestMaintenanceSurvivesTheScrapeLoop:
    """维护异常必须原样穿过 ``_scrape_city_pages``，不能被改判成网络错误。

    2026-08-04 / 08-15 生产实测共 20 次误判，日志长这样——一句话里同时写着
    「网络错误」和「平台维护中」::

        [ERROR] holland2stay:Eindhoven 抓取网络失败，已隔离该任务:
                [Eindhoven] 第 1 页网络错误: H2S 平台维护中（页面标题: H2S-Maintenance）

    成因是重抛白名单漏了 ``UpstreamMaintenanceError``，于是它落进下面那条
    ``except Exception`` 被包成 ``ScrapeNetworkError``。

    讽刺的是 ``browser_fetcher.fetch()`` 里专门为这件事写过一段注释——
    「这是维护，不是屏蔽。压成 BlockedError 会让 monitor 走熔断 + admin 告警，
    而不是安静的维护冷却」——它小心翼翼把异常原样抛出来，下一层就给压掉了。

    代价：维护期走的是网络失败路径（5 分钟冷却 + 连续失败计数 + ERROR 日志），
    而不是设计好的维护路径（15 分钟安静冷却 + INFO + 不发用户告警 + dashboard
    banner）。

    ``XiorScraper._post_ajax`` 的同一个位置一直是对的
    （``except (BlockedError, UpstreamMaintenanceError): raise``）。
    """

    @staticmethod
    def _run(exc):
        import scrapers.holland2stay as h2s

        class _Fetcher:
            def fetch_gql(self, *a, **k):
                raise exc

        return h2s._scrape_city_pages(
            _Fetcher(), "Eindhoven", city_ids=["29"],
            availability_ids=["179"], attr_labels={},
        )

    def test_maintenance_is_not_reclassified_as_network_error(self):
        from scrapers.base import ScrapeNetworkError

        with pytest.raises(UpstreamMaintenanceError):
            self._run(UpstreamMaintenanceError("H2S 平台维护中（页面标题: H2S-Maintenance）"))

        # 反向确认：它没有变成 ScrapeNetworkError
        try:
            self._run(UpstreamMaintenanceError("维护"))
        except ScrapeNetworkError as e:                     # pragma: no cover
            pytest.fail(f"维护被改判成网络错误了: {e}")
        except UpstreamMaintenanceError:
            pass

    def test_the_message_does_not_say_network(self):
        """回归到文案层：日志里不该再出现「网络错误: …平台维护中」这种自相矛盾。"""
        try:
            self._run(UpstreamMaintenanceError("H2S 平台维护中"))
        except Exception as e:
            assert "网络错误" not in str(e), (
                f"维护异常的消息里还写着「网络错误」: {e}"
            )

    @pytest.mark.parametrize("exc_name", [
        "RateLimitError", "BlockedError", "OperationNotAllowedError",
        "ScrapeNetworkError", "UpstreamMaintenanceError",
    ])
    def test_every_taxonomy_error_passes_through_unchanged(self, exc_name):
        """整个 taxonomy 都必须原样穿过——漏掉任何一个都是同一类 bug。

        判据很简单：这些类各自代表一种**上层要据以做不同决策**的根因
        （换 IP / 等冷却 / 照抄 operation / 安静等维护 / 查代理）。在这里把
        任何一个压成别的类，上层那套决策就全废了。
        """
        import scrapers.base as base

        cls = getattr(base, exc_name)
        with pytest.raises(cls):
            self._run(cls("boom"))

    def test_unexpected_errors_still_become_network_errors(self):
        """反向守卫：taxonomy 之外的异常（我们自己的 bug）仍然归网络类。

        不是说这个归类有多准，而是上层的隔离与重试语义依赖它，别顺手改掉。
        """
        from scrapers.base import ScrapeNetworkError

        with pytest.raises(ScrapeNetworkError):
            self._run(TypeError("解析炸了"))
