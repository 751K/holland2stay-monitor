"""
403 / Cloudflare 屏蔽处理测试。

生产事故：H2S API 返回 403（Cloudflare WAF），旧代码静默 break，
monitor 每 3-5 分钟刷错误日志几小时，用户不知道，无法行动。

新行为契约：
1. scraper._post_gql 检测 403 → 立刻抛 BlockedError，附带可操作的建议
2. _post_gql 能识别 Cloudflare 挑战页 vs 其他 403
3. _scrape_city_pages / scrape_all 把 BlockedError 透传上去（不当普通 Exception）
4. monitor.run_once 捕获 BlockedError → ERROR 日志 + 通过用户渠道通知（节流）+ re-raise
5. monitor.main_loop 捕获 BlockedError → 15 分钟冷却（vs 429 的 5 分钟）
6. 节流：30 分钟内最多发一次屏蔽通知，避免持续屏蔽刷屏
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import monitor
from monitor import _clear_prewarm_cache, run_once
from scraper import BlockedError, RateLimitError, ScrapeNetworkError
from booker import BookingResult
from notifier import BaseNotifier
from users import UserConfig
from config import AutoBookConfig, Config, CityFilter, AvailabilityFilter
from storage import Storage


# Cloudflare 挑战页的典型片段（user 提供的真实样本）
_CLOUDFLARE_HTML = (
    '<!DOCTYPE html>\n'
    '<!--[if lt IE 7]> <html class="no-js ie6 oldie" lang="en-US"> <![endif]-->\n'
    '<!--[if IE 7]>    <html class="no-js ie7 oldie" lang="en-US"> <![endif]-->\n'
)


def _fake_response(status_code, body=""):
    """构造 curl_cffi 风格的响应 mock。"""
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


# ─── scraper._post_gql 单元测试 ─────────────────────────────────


class TestPostGqlBlockedError:
    """403 应该立刻抛 BlockedError，不重试（与 429 不同）。"""

    def test_403_with_cloudflare_html_raises_blocked(self):
        from scraper import _post_gql

        session = MagicMock()
        session.post.return_value = _fake_response(403, _CLOUDFLARE_HTML)

        with pytest.raises(BlockedError) as excinfo:
            _post_gql(session, "query{}")

        # 错误消息必须告知是 Cloudflare WAF + 给出可操作建议
        msg = str(excinfo.value)
        assert "Cloudflare WAF" in msg, f"未识别 Cloudflare: {msg}"
        assert "HTTPS_PROXY" in msg, f"未给出代理建议: {msg}"
        assert "重启" in msg, f"未给出重启建议: {msg}"

    def test_403_non_cloudflare_still_raises_blocked(self):
        """非 Cloudflare 的 403（API 自己返的）也走 BlockedError 路径。"""
        from scraper import _post_gql

        session = MagicMock()
        session.post.return_value = _fake_response(403, '{"error": "forbidden"}')

        with pytest.raises(BlockedError) as excinfo:
            _post_gql(session, "query{}")

        # 消息应该区分 "API 拒绝服务"
        assert "API 拒绝服务" in str(excinfo.value) or "403" in str(excinfo.value)

    def test_403_does_not_retry(self):
        """403 应该立刻抛，不进 _RATE_LIMIT_BACKOFF 重试循环。"""
        from scraper import _post_gql

        session = MagicMock()
        session.post.return_value = _fake_response(403, _CLOUDFLARE_HTML)

        with pytest.raises(BlockedError):
            _post_gql(session, "query{}")

        # session.post 只应被调用一次（无重试）
        assert session.post.call_count == 1

    def test_429_still_works_unchanged(self):
        """回归保护：429 走原有重试路径，不被 403 改动影响。"""
        from scraper import _post_gql

        session = MagicMock()
        # 一直返回 429 → 应该重试 + 最终抛 RateLimitError
        session.post.return_value = _fake_response(429, "")

        with patch("scraper.time.sleep"):  # 不真睡
            with pytest.raises(RateLimitError):
                _post_gql(session, "query{}")

        # 应该尝试了至少 3 次（首次 + 2 次退避）
        assert session.post.call_count >= 3

    def test_200_normal_response(self):
        """回归：正常 200 响应不受影响。"""
        from scraper import _post_gql

        session = MagicMock()
        good = _fake_response(200, '')
        good.json = MagicMock(return_value={"data": {"products": {"items": []}}})
        session.post.return_value = good

        result = _post_gql(session, "query{}")
        assert "data" in result
        assert session.post.call_count == 1


# ─── BlockedError 传播测试 ──────────────────────────────────────


class TestBlockedErrorPropagation:
    """403 必须从 _post_gql 一路传到 scrape_all，不被中间层吞。"""

    def test_blocked_error_propagates_through_scrape_city_pages(self):
        """_scrape_city_pages 不能 except Exception 把 BlockedError 吃掉。"""
        from scraper import _scrape_city_pages

        with patch("scraper._post_gql", side_effect=BlockedError("test")):
            with pytest.raises(BlockedError):
                _scrape_city_pages(MagicMock(), "Eindhoven", ["29"], ["179"])

    def test_blocked_error_propagates_through_scrape_all(self):
        """scrape_all 也必须透传，不能降级为单城市失败。"""
        from scraper import scrape_all

        with patch("scraper._scrape_city_pages", side_effect=BlockedError("test")):
            with pytest.raises(BlockedError):
                scrape_all([("Eindhoven", "29")], ["179"])


# ─── ScrapeNetworkError 传播测试 ─────────────────────────────────


class TestScrapeNetworkErrorPropagation:
    """第一页网络失败必须上传，避免被当成 0 条有效抓取。"""

    def test_first_page_network_error_raises_scrape_network_error(self):
        """第 1 页 timeout/TLS/连接失败不能被 break 吞掉。"""
        from scraper import _scrape_city_pages

        with patch("scraper._post_gql", side_effect=TimeoutError("timeout")):
            with pytest.raises(ScrapeNetworkError) as excinfo:
                _scrape_city_pages(MagicMock(), "Eindhoven", ["29"], ["179"])

        assert "第 1 页网络错误" in str(excinfo.value)

    def test_later_page_network_error_returns_previous_pages(self):
        """第 2 页之后失败可以返回已抓到的前面分页。"""
        from scraper import _scrape_city_pages

        first_page = {
            "data": {
                "products": {
                    "items": [],
                    "page_info": {"current_page": 1, "total_pages": 2},
                }
            }
        }
        with patch("scraper._post_gql", side_effect=[first_page, TimeoutError("timeout")]):
            result = _scrape_city_pages(MagicMock(), "Eindhoven", ["29"], ["179"])

        assert result == []

    def test_scrape_all_raises_when_all_cities_fail_on_first_page(self):
        """所有城市第 1 页都网络失败时，整体抓取必须失败并交给 monitor cooldown。"""
        from scraper import scrape_all

        with patch(
            "scraper._scrape_city_pages",
            side_effect=ScrapeNetworkError("page 1 failed"),
        ):
            with pytest.raises(ScrapeNetworkError) as excinfo:
                scrape_all([("Eindhoven", "29"), ("Amsterdam", "24")], ["179"])

        assert "全部 2 个城市第 1 页网络失败" in str(excinfo.value)


# ─── monitor 的 BlockedError 处理 ─────────────────────────────


class TestMonitorBlockedHandling:
    """run_once 必须将 BlockedError 当一等异常处理：通知 + re-raise。"""

    def setup_method(self):
        # 重置通知节流，每个测试独立
        monitor._last_block_notify_at = 0.0
        _clear_prewarm_cache()

    def teardown_method(self):
        monitor._last_block_notify_at = 0.0
        _clear_prewarm_cache()

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
            with patch("monitor.scrape_all", side_effect=scrape_fn):
                await run_once(cfg, storage, notifs, dry_run=False)

        try:
            asyncio.run(go())
        finally:
            storage.close()

    def test_run_once_reraises_blocked_error(self, tmp_path):
        """run_once 必须 re-raise 让 main_loop 应用 cooldown。"""
        user = UserConfig(
            name="A", id="aaaa", enabled=True, notifications_enabled=True,
            notification_channels=[],
            auto_book=AutoBookConfig(enabled=False),
        )
        scrape = lambda *a, **k: (_ for _ in ()).throw(BlockedError("cf block"))

        with pytest.raises(BlockedError):
            self._run(tmp_path, scrape, user)

    def test_run_once_notifies_user_on_block(self, tmp_path):
        """首次屏蔽必须给用户发通知（通过其配置的通知渠道）。"""
        user = UserConfig(
            name="A", id="aaaa", enabled=True, notifications_enabled=True,
            notification_channels=[],
            auto_book=AutoBookConfig(enabled=False),
        )
        scrape = lambda *a, **k: (_ for _ in ()).throw(BlockedError("Cloudflare WAF 屏蔽（HTTP 403）"))
        notifs_received: list[str] = []

        with pytest.raises(BlockedError):
            self._run(tmp_path, scrape, user, notifs_received)

        assert len(notifs_received) == 1, "首次屏蔽必须发 1 条通知"
        assert "403" in notifs_received[0]
        assert "Cloudflare" in notifs_received[0] or "屏蔽" in notifs_received[0]

    def test_notification_throttle_30min(self, tmp_path):
        """30 分钟内多次屏蔽，只发 1 条通知（避免刷屏）。"""
        user = UserConfig(
            name="A", id="aaaa", enabled=True, notifications_enabled=True,
            notification_channels=[],
            auto_book=AutoBookConfig(enabled=False),
        )
        scrape = lambda *a, **k: (_ for _ in ()).throw(BlockedError("test"))
        notifs_received: list[str] = []

        # 连续触发 3 次屏蔽
        for _ in range(3):
            with pytest.raises(BlockedError):
                self._run(tmp_path, scrape, user, notifs_received)

        assert len(notifs_received) == 1, (
            f"30 分钟内多次屏蔽应该只发 1 通知，实际 {len(notifs_received)}"
        )

    def test_notification_unthrottled_after_interval(self, tmp_path):
        """超过节流间隔后，应该再次允许通知。"""
        user = UserConfig(
            name="A", id="aaaa", enabled=True, notifications_enabled=True,
            notification_channels=[],
            auto_book=AutoBookConfig(enabled=False),
        )
        scrape = lambda *a, **k: (_ for _ in ()).throw(BlockedError("test"))
        notifs_received: list[str] = []

        # 第一次
        with pytest.raises(BlockedError):
            self._run(tmp_path, scrape, user, notifs_received)
        assert len(notifs_received) == 1

        # 模拟时间过去 31 分钟（移动节流戳）
        monitor._last_block_notify_at -= 31 * 60

        # 第二次应该重新发
        with pytest.raises(BlockedError):
            self._run(tmp_path, scrape, user, notifs_received)
        assert len(notifs_received) == 2, "超过 30 分钟后应该重新通知"


# ─── _should_notify_block 单元测试 ────────────────────────────


class TestShouldNotifyBlock:
    def setup_method(self):
        monitor._last_block_notify_at = 0.0

    def teardown_method(self):
        monitor._last_block_notify_at = 0.0

    def test_first_call_returns_true(self):
        from monitor import _should_notify_block
        assert _should_notify_block() is True

    def test_second_call_within_interval_returns_false(self):
        from monitor import _should_notify_block
        assert _should_notify_block() is True
        assert _should_notify_block() is False
        assert _should_notify_block() is False  # 持续返回 False

    def test_call_after_interval_returns_true_again(self):
        from monitor import _should_notify_block, _BLOCK_NOTIFY_INTERVAL
        _should_notify_block()  # 消耗首次
        # 把时间戳推回过去（模拟时间流逝）
        monitor._last_block_notify_at -= (_BLOCK_NOTIFY_INTERVAL + 1)
        assert _should_notify_block() is True

    def test_interval_is_substantial(self):
        """节流间隔必须 >= 15 分钟，否则失去节流意义。"""
        from monitor import _BLOCK_NOTIFY_INTERVAL
        assert _BLOCK_NOTIFY_INTERVAL >= 900, "节流间隔太短，会刷屏"
