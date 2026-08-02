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


class TestPerSourceStickySession:
    """各 source 用独立的 sticky session，避免共享出口 IP 的限流额度。

    背景（2026-08-02 实测）：换成 sticky 代理后所有 source 挤在同一个出口 IP，
    Xior 一轮 12 个请求触发 429，四栋楼全部失败。而出口 IP 稳定又是 Cloudflare
    clearance 能复用的前提，不能退回每请求轮换——所以按 source 分 session。
    """

    STICKY = "http://acct-nl-790346:pw@p.webshare.io:80"

    def test_each_source_gets_a_distinct_session(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", self.STICKY)
        urls = {s: config.get_proxy_url(s)
                for s in ("holland2stay", "xior", "ourdomain")}
        assert len(set(urls.values())) == 3, urls
        for u in urls.values():
            assert u.startswith("http://acct-nl-")
            assert "@p.webshare.io:80" in u

    def test_session_is_stable_across_calls(self, monkeypatch):
        """同一 source 每次都要拿到同一个 session，否则出口 IP 稳不住。"""
        monkeypatch.setenv("HTTPS_PROXY", self.STICKY)
        assert config.get_proxy_url("xior") == config.get_proxy_url("xior")

    def test_no_source_returns_base_url_unchanged(self, monkeypatch):
        """不传 source 时保持原样——monitor / doctor 只判断有没有配代理。"""
        monkeypatch.setenv("HTTPS_PROXY", self.STICKY)
        assert config.get_proxy_url() == self.STICKY

    def test_credentials_are_preserved(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", self.STICKY)
        assert ":pw@" in config.get_proxy_url("xior")

    @pytest.mark.parametrize("url", [
        "http://acct-us-rotate:pw@p.webshare.io:80",   # 用户明确要求每请求轮换
        "http://plainuser:pw@proxy.local:8080",        # 没有 session 段
        "http://proxy.local:8080",                     # 无鉴权
    ])
    def test_unrecognised_shapes_pass_through(self, monkeypatch, url):
        """只在用户名已以数字 session 结尾时才替换。

        凭空拼接可能被 webshare 解析成国家码之类，反而把配置搞坏。
        """
        monkeypatch.setenv("HTTPS_PROXY", url)
        assert config.get_proxy_url("xior") == url

    def test_cooldown_still_applies_with_source(self, monkeypatch):
        """故障切换逻辑不受 source 影响。"""
        monkeypatch.setenv("HTTPS_PROXY", self.STICKY)
        config.report_proxy_failure()
        config.report_proxy_failure()
        assert config.get_proxy_url("xior") == ""
