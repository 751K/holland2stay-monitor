"""
代理池 + 故障切换测试（config.get_proxy_url / report_proxy_failure）。

主代理挂了（webshare 502）自动切到 SCRAPE_PROXIES_FALLBACK 里的备用，
故障代理进 10 min 冷却，冷却结束自动重新纳入。
"""
from __future__ import annotations

import importlib
import pytest

import config


@pytest.fixture(autouse=True)
def clean_proxy_env(monkeypatch):
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "SCRAPE_PROXIES_FALLBACK"):
        monkeypatch.delenv(k, raising=False)
    config._proxy_cooldown_until.clear()
    yield
    config._proxy_cooldown_until.clear()


class TestProxyPool:
    def test_no_proxy_returns_empty(self):
        assert config.get_proxy_url() == ""
        assert config.proxy_pool_size() == 0

    def test_primary_only(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://primary:1")
        assert config.get_proxy_url() == "http://primary:1"
        assert config.proxy_pool_size() == 1

    def test_pool_dedup_and_order(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://p:1")
        monkeypatch.setenv("SCRAPE_PROXIES_FALLBACK", "http://b1:2, http://p:1 , http://b2:3")
        # 去重（p:1 只出现一次）+ 保序
        assert config._proxy_pool() == ["http://p:1", "http://b1:2", "http://b2:3"]

    def test_failover_chain(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://p:1")
        monkeypatch.setenv("SCRAPE_PROXIES_FALLBACK", "http://b1:2,http://b2:3")
        assert config.get_proxy_url() == "http://p:1"
        assert config.report_proxy_failure() == "http://b1:2"   # p 挂 → b1
        assert config.report_proxy_failure() == "http://b2:3"   # b1 挂 → b2

    def test_all_cooled_returns_soonest(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://p:1")
        monkeypatch.setenv("SCRAPE_PROXIES_FALLBACK", "http://b1:2")
        config.report_proxy_failure()  # cool p
        config.report_proxy_failure()  # cool b1 (current after p cooled)
        # 全冷却 → 仍返回一个（最早恢复的），不返回空
        assert config.get_proxy_url() in ("http://p:1", "http://b1:2")

    def test_cooldown_expires(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://p:1")
        monkeypatch.setenv("SCRAPE_PROXIES_FALLBACK", "http://b1:2")
        config.report_proxy_failure()  # cool p
        assert config.get_proxy_url() == "http://b1:2"
        # 手动把 p 的冷却拨到过去 → 重新可用，回到主代理优先
        config._proxy_cooldown_until["http://p:1"] = 0.0
        assert config.get_proxy_url() == "http://p:1"

    def test_report_specific_url(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://p:1")
        monkeypatch.setenv("SCRAPE_PROXIES_FALLBACK", "http://b1:2")
        config.report_proxy_failure("http://p:1")
        assert config.get_proxy_url() == "http://b1:2"
