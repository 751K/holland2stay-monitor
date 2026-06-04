"""
代理池 + 故障切换测试（config.get_proxy_url / report_proxy_failure）。

主代理挂了（webshare 502）自动切到 SCRAPE_PROXIES_FALLBACK 里的备用，
连续确认故障的代理进 10 min 冷却，冷却结束自动重新纳入；若所有代理都在
冷却，抓取层降级为服务器原生 IP 直连，monitor 再把轮询频率压到最多
10 min 一次。
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
    config._proxy_failure_marks.clear()
    yield
    config._proxy_cooldown_until.clear()
    config._proxy_failure_marks.clear()


class TestProxyPool:
    def test_no_proxy_returns_empty(self):
        assert config.get_proxy_url() == ""
        assert config.proxy_pool_size() == 0
        assert config.is_proxy_native_fallback_active() is False

    def test_primary_only(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://primary:1")
        assert config.get_proxy_url() == "http://primary:1"
        assert config.proxy_pool_size() == 1
        assert config.is_proxy_native_fallback_active() is False

    def test_pool_dedup_and_order(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://p:1")
        monkeypatch.setenv("SCRAPE_PROXIES_FALLBACK", "http://b1:2, http://p:1 , http://b2:3")
        # 去重（p:1 只出现一次）+ 保序
        assert config._proxy_pool() == ["http://p:1", "http://b1:2", "http://b2:3"]

    def test_failover_chain(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://p:1")
        monkeypatch.setenv("SCRAPE_PROXIES_FALLBACK", "http://b1:2,http://b2:3")
        assert config.get_proxy_url() == "http://p:1"
        assert config.report_proxy_failure() == "http://p:1"    # 第 1 次只记录
        assert config.report_proxy_failure() == "http://b1:2"   # 第 2 次确认 p 挂 → b1
        assert config.report_proxy_failure() == "http://b1:2"   # 第 1 次只记录 b1
        assert config.report_proxy_failure() == "http://b2:3"   # 第 2 次确认 b1 挂 → b2

    def test_all_cooled_enters_native_fallback(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://p:1")
        monkeypatch.setenv("SCRAPE_PROXIES_FALLBACK", "http://b1:2")
        config.report_proxy_failure()  # mark p
        config.report_proxy_failure()  # cool p
        config.report_proxy_failure()  # mark b1
        config.report_proxy_failure()  # cool b1
        # 全冷却 → 返回空代理，让 scraper 直连原生 IP；monitor 负责降频。
        assert config.get_proxy_url() == ""
        assert config.is_proxy_native_fallback_active() is True

    def test_cooldown_expires(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://p:1")
        monkeypatch.setenv("SCRAPE_PROXIES_FALLBACK", "http://b1:2")
        config.report_proxy_failure()  # mark p
        config.report_proxy_failure()  # cool p
        assert config.get_proxy_url() == "http://b1:2"
        # 手动把 p 的冷却拨到过去 → 重新可用，回到主代理优先
        config._proxy_cooldown_until["http://p:1"] = 0.0
        assert config.get_proxy_url() == "http://p:1"
        assert config.is_proxy_native_fallback_active() is False

    def test_report_specific_url(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://p:1")
        monkeypatch.setenv("SCRAPE_PROXIES_FALLBACK", "http://b1:2")
        config.report_proxy_failure("http://p:1")
        assert config.get_proxy_url() == "http://p:1"
        assert config.proxy_failure_mark_count("http://p:1") == 1
        config.report_proxy_failure("http://p:1")
        assert config.get_proxy_url() == "http://b1:2"

    def test_primary_only_failure_falls_back_to_native(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://p:1")
        assert config.report_proxy_failure() == "http://p:1"
        assert config.get_proxy_url() == "http://p:1"
        assert config.is_proxy_native_fallback_active() is False
        assert config.proxy_failure_mark_count("http://p:1") == 1
        assert config.report_proxy_failure() == ""
        assert config.get_proxy_url() == ""
        assert config.is_proxy_native_fallback_active() is True

    def test_unconfirmed_failure_does_not_fallback(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://p:1")
        assert config.report_proxy_failure(service_error_confirmed=False) == "http://p:1"
        assert config.report_proxy_failure(service_error_confirmed=False) == "http://p:1"
        assert config.get_proxy_url() == "http://p:1"
        assert config.proxy_failure_mark_count("http://p:1") == 2
        assert config.is_proxy_native_fallback_active() is False
