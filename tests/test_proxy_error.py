"""
代理失效检测 + 上抛测试。

webshare 等抓取代理挂掉时返回 502 CONNECT，curl_cffi 抛 ProxyError。
之前只会默默进网络冷却，dashboard 不报警。现在归类成 ProxyError（仍是
ScrapeNetworkError 子类，控制流不变），monitor 据此发"代理失效"admin 告警。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import scrapers
from scrapers.base import (
    ProxyError,
    ScrapeNetworkError,
    ScrapeTask,
    is_proxy_error,
)


class TestIsProxyError:
    @pytest.mark.parametrize("msg, expected", [
        ("Failed to perform, curl: (56) CONNECT tunnel failed, response 502", True),
        ("Proxy CONNECT aborted", True),
        ("Tunnel connection failed: 502 Bad Gateway", True),
        ("curl: (97) proxy handshake", True),
        ("Connection timed out", False),
        ("TLS handshake failed", False),
    ])
    def test_message_detection(self, msg, expected):
        assert is_proxy_error(Exception(msg)) is expected

    def test_class_name_detection(self):
        class ProxyError(Exception):  # 模拟 curl_cffi 的 ProxyError
            pass
        assert is_proxy_error(ProxyError("anything")) is True


class TestProxyErrorIsNetworkSubclass:
    def test_subclass(self):
        # ProxyError 必须是 ScrapeNetworkError 子类——沿用网络冷却控制流
        assert issubclass(ProxyError, ScrapeNetworkError)
        e = ProxyError("x")
        assert isinstance(e, ScrapeNetworkError)


class _ProxyDownScraper(scrapers.AbstractScraper):
    source = "holland2stay"

    def scrape(self, task):
        raise ProxyError("curl: (56) CONNECT tunnel failed, response 502")


class TestDispatchRaisesProxyError:
    def test_all_proxy_fail_raises_proxy_error(self, monkeypatch):
        monkeypatch.setitem(scrapers.SCRAPER_REGISTRY, "holland2stay", _ProxyDownScraper)
        tasks = [
            ScrapeTask(source="holland2stay", city_key="29", city_display="Eindhoven"),
            ScrapeTask(source="holland2stay", city_key="24", city_display="Amsterdam"),
        ]
        with pytest.raises(ProxyError) as ei:
            scrapers.dispatch_scrape_tasks(tasks)
        assert "代理故障" in str(ei.value)

    def test_proxy_error_caught_as_network_error(self, monkeypatch):
        """旧的 except ScrapeNetworkError 仍能兜住 ProxyError（控制流不破）。"""
        monkeypatch.setitem(scrapers.SCRAPER_REGISTRY, "holland2stay", _ProxyDownScraper)
        tasks = [ScrapeTask(source="holland2stay", city_key="29", city_display="Eindhoven")]
        with pytest.raises(ScrapeNetworkError):  # ProxyError 是子类
            scrapers.dispatch_scrape_tasks(tasks)
