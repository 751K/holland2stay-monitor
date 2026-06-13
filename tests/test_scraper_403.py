"""
403 / Cloudflare 屏蔽处理测试（适配新 CloakBrowser 路径）。

旧 curl_cffi _post_gql 已删除；现在浏览器内 fetch 检测 403 并通过
BlockedError 向上传播。monitor 级别的 circuit breaker + 通知节流不受影响。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import monitor
from monitor import run_once
from scraper import BlockedError, RateLimitError, ScrapeNetworkError
from booker import BookingResult
from notifier import BaseNotifier
from users import UserConfig
from config import AutoBookConfig, Config, CityFilter, AvailabilityFilter
from storage import Storage


_PATCH_SCRAPE = "scrapers.holland2stay._scrape_city_pages"


def _make_fetcher(*responses):
    """构造带 fetch_gql 响应的 mock fetcher。"""
    fetcher = MagicMock()
    fetcher.fetch_gql.side_effect = list(responses) if len(responses) > 1 else responses
    return fetcher


# ─── BlockedError 传播测试 ──────────────────────────────────────

def _h2s_tasks(*pairs):
    from scrapers.base import ScrapeTask
    return [
        ScrapeTask(
            source="holland2stay",
            city_key=city_id,
            city_display=city_name,
            extra={"availability_ids": ["179"]},
        )
        for city_name, city_id in pairs
    ]


class TestBlockedErrorPropagation:
    """BlockedError 必须从 scraper 一路传到 dispatcher，不被中间层吞。"""

    def test_blocked_error_propagates_through_scrape_city_pages(self):
        from scrapers.holland2stay import _scrape_city_pages

        fetcher = _make_fetcher(BlockedError("test"))
        with pytest.raises(BlockedError):
            _scrape_city_pages(fetcher, "Eindhoven", ["29"], ["179"], {})

    def test_blocked_error_propagates_through_dispatch(self):
        from scrapers import dispatch_scrape_tasks

        with patch(_PATCH_SCRAPE, side_effect=BlockedError("test")), \
             patch("scrapers.holland2stay.BrowserFetcher", return_value=MagicMock()):
            with pytest.raises(BlockedError):
                dispatch_scrape_tasks(_h2s_tasks(("Eindhoven", "29")))


# ─── ScrapeNetworkError 传播测试 ─────────────────────────────────

class TestScrapeNetworkErrorPropagation:
    def test_first_page_network_error_raises_scrape_network_error(self):
        from scrapers.holland2stay import _scrape_city_pages

        fetcher = _make_fetcher(ScrapeNetworkError("network fail"))
        with pytest.raises(ScrapeNetworkError):
            _scrape_city_pages(fetcher, "Eindhoven", ["29"], ["179"], {})

    def test_later_page_network_error_returns_previous_pages(self):
        from scrapers.holland2stay import _scrape_city_pages

        first_page = {
            "data": {
                "products": {
                    "items": [],
                    "page_info": {"current_page": 1, "total_pages": 2},
                }
            }
        }
        fetcher = _make_fetcher(first_page, TimeoutError("timeout"))
        result, complete = _scrape_city_pages(fetcher, "Eindhoven", ["29"], ["179"], {})

        assert result == []
        assert complete is False

    def test_dispatch_raises_when_all_cities_fail_on_first_page(self):
        from scrapers import dispatch_scrape_tasks

        with patch(_PATCH_SCRAPE, side_effect=ScrapeNetworkError("page 1 failed")), \
             patch("scrapers.holland2stay.BrowserFetcher", return_value=MagicMock()):
            with pytest.raises(ScrapeNetworkError) as excinfo:
                dispatch_scrape_tasks(
                    _h2s_tasks(("Eindhoven", "29"), ("Amsterdam", "24"))
                )

        assert "全部 2 个任务网络失败" in str(excinfo.value)


# ─── monitor 的 BlockedError 处理（不变：circuit breaker + 通知节流）───

class TestMonitorBlockedHandling:
    def setup_method(self):
        monitor._last_block_notify_at = 0.0
        monitor.prewarm_cache.clear()

    def teardown_method(self):
        monitor._last_block_notify_at = 0.0
        monitor.prewarm_cache.clear()

    def _run(self, tmp_path, scrape_fn, user, notifications_capture=None):
        cfg = Config(
            check_interval=300,
            cities=[CityFilter(name="E", id=29)],
            availability_filters=[AvailabilityFilter(label="A", id=179)],
            db_path=tmp_path / "test.db", log_level="WARNING",
        )

        class CapturingNotifier(BaseNotifier):
            has_channels = True
            async def _send(self, t):
                if notifications_capture is not None:
                    notifications_capture.append(t)
                return True
            async def close(self): pass

        notifs = [(user, CapturingNotifier())]
        storage = Storage(tmp_path / "test.db", timezone_str="UTC")

        async def go():
            with patch("monitor.dispatch_scrape_tasks", side_effect=scrape_fn):
                await run_once(cfg, storage, notifs, dry_run=False)

        try:
            asyncio.run(go())
        finally:
            storage.close()

    def test_run_once_opens_h2s_circuit_on_blocked_error(self, tmp_path):
        user = UserConfig(
            name="A", id="aaaa", enabled=True, notifications_enabled=True,
            notification_channels=[],
            auto_book=AutoBookConfig(enabled=False),
        )
        scrape = lambda *a, **k: (_ for _ in ()).throw(BlockedError("cf block"))

        self._run(tmp_path, scrape, user)

        assert monitor._h2s_circuit_fail_streak == 1
        assert monitor._h2s_circuit_open_until > 0

    def test_run_once_notifies_user_on_block(self, tmp_path):
        user = UserConfig(
            name="A", id="aaaa", enabled=True, notifications_enabled=True,
            notification_channels=[],
            auto_book=AutoBookConfig(enabled=False),
        )
        scrape = lambda *a, **k: (_ for _ in ()).throw(
            BlockedError("Cloudflare WAF 屏蔽（HTTP 403）")
        )
        notifs_received: list[str] = []

        self._run(tmp_path, scrape, user, notifs_received)

        assert len(notifs_received) == 1, "首次屏蔽必须发 1 条通知"
        assert "403" in notifs_received[0]
        assert "H2S" in notifs_received[0]

    def test_notification_throttle_30min(self, tmp_path):
        user = UserConfig(
            name="A", id="aaaa", enabled=True, notifications_enabled=True,
            notification_channels=[],
            auto_book=AutoBookConfig(enabled=False),
        )
        scrape = lambda *a, **k: (_ for _ in ()).throw(BlockedError("test"))
        notifs_received: list[str] = []

        for _ in range(3):
            self._run(tmp_path, scrape, user, notifs_received)
            monitor._h2s_circuit_open_until = 0.0

        assert len(notifs_received) == 1, (
            f"30 分钟内多次屏蔽应该只发 1 通知，实际 {len(notifs_received)}"
        )

    def test_notification_unthrottled_after_interval(self, tmp_path):
        user = UserConfig(
            name="A", id="aaaa", enabled=True, notifications_enabled=True,
            notification_channels=[],
            auto_book=AutoBookConfig(enabled=False),
        )
        scrape = lambda *a, **k: (_ for _ in ()).throw(BlockedError("test"))
        notifs_received: list[str] = []

        self._run(tmp_path, scrape, user, notifs_received)
        assert len(notifs_received) == 1

        monitor._last_block_notify_at -= 31 * 60
        monitor._h2s_circuit_open_until = 0.0

        self._run(tmp_path, scrape, user, notifs_received)
        assert len(notifs_received) == 2, "超过 30 分钟后应该重新通知"

    def test_long_h2s_block_notifies_admin_to_check_server(self, tmp_path):
        user = UserConfig(
            name="A", id="aaaa", enabled=True, notifications_enabled=True,
            notification_channels=[],
            auto_book=AutoBookConfig(enabled=False),
        )
        cfg = Config(
            check_interval=300,
            cities=[CityFilter(name="E", id=29)],
            availability_filters=[AvailabilityFilter(label="A", id=179)],
            db_path=tmp_path / "test.db", log_level="WARNING",
        )
        storage = Storage(tmp_path / "test.db", timezone_str="UTC")

        class CapturingNotifier(BaseNotifier):
            has_channels = True
            async def _send(self, t): return True
            async def close(self): pass

        class CapturingAdmin:
            def __init__(self):
                self.errors: list[str] = []
            async def send_error(self, msg):
                self.errors.append(msg)
                return True

        admin = CapturingAdmin()
        scrape = lambda *a, **k: (_ for _ in ()).throw(
            BlockedError("Cloudflare WAF 屏蔽（HTTP 403）")
        )

        async def go():
            with patch("monitor.dispatch_scrape_tasks", side_effect=scrape), \
                 patch("mcore.push.dispatch_admin", new=AsyncMock(return_value=1)):
                for _ in range(3):
                    await run_once(
                        cfg, storage,
                        [(user, CapturingNotifier())],
                        web_notifier=admin, dry_run=False,
                    )
                    monitor._h2s_circuit_open_until = 0.0

        try:
            asyncio.run(go())
        finally:
            storage.close()

        long_block_msgs = [
            msg for msg in admin.errors
            if "H2S 长时间被 block" in msg
        ]
        assert len(long_block_msgs) == 1
        assert "需要检查服务器" in long_block_msgs[0]
