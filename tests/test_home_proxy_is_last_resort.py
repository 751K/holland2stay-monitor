"""家庭 IP 只在两个商业代理都失效之后才启用。

背景（2026-08-26）
------------------
webshare 的两个账号（主 + 备）同时返回 402，代理池整体归零，全站停摆两小时。
补救方案是把一台家里常开的机器接成第三个代理——它的出口是自家住宅宽带，
Cloudflare 对它的评分和商业住宅池同级，H2S / Xior 因此能过。

但它**必须排在最后**，理由不是技术而是取舍：那是用户自己的家庭宽带，
出现在平台访问日志里的是他家的 IP。商业代理还能用的时候就不该动用它。

本文件钉的就是这个顺序。实现上不需要新代码——``_proxy_pool()`` 保序、
``get_proxy_url()`` 取第一个不在冷却的，天然满足；但「天然满足」这件事
必须有测试，否则哪天有人给池子加个排序或去重，这条约束会静默消失。
"""
from __future__ import annotations

import pytest

import config

P1 = "http://u1-nl-111111:pw@p.webshare.io:80"      # 主
P2 = "http://u2-nl-222222:pw@p.webshare.io:80"      # 备
HOME = "http://127.0.0.1:3128"                       # 家里那台，无凭据


@pytest.fixture
def pool(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", P1)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("ALL_PROXY", raising=False)
    monkeypatch.setenv("SCRAPE_PROXIES_FALLBACK", f"{P2},{HOME}")
    config._proxy_cooldown_until.clear()
    config._proxy_failure_marks.clear()
    yield
    config._proxy_cooldown_until.clear()
    config._proxy_failure_marks.clear()


def _cool(url: str) -> None:
    """把某个代理按下冷却，绕开「连续两次确认」的门槛直接置状态。"""
    import time
    config._proxy_cooldown_until[url] = time.monotonic() + config._PROXY_COOLDOWN_SEC


class TestOrdering:
    def test_pool_is_primary_then_fallback_then_home(self, pool):
        assert config._proxy_pool() == [P1, P2, HOME]

    def test_home_untouched_while_primary_is_fine(self, pool):
        assert config.get_proxy_url() == P1

    def test_still_not_home_when_only_primary_is_down(self, pool):
        _cool(P1)
        assert config.get_proxy_url() == P2, "备用还活着就不该动家里的线路"

    def test_home_kicks_in_only_after_both_are_down(self, pool):
        _cool(P1)
        _cool(P2)
        assert config.get_proxy_url() == HOME

    def test_recovers_to_commercial_when_they_come_back(self, pool):
        _cool(P1)
        _cool(P2)
        assert config.get_proxy_url() == HOME
        config._proxy_cooldown_until.clear()          # 充值/续费之后
        assert config.get_proxy_url() == P1, "商业代理恢复后必须让回去"


class TestHomeProxyUrlSurvivesRewriting:
    """``_with_source_session`` 会改写用户名派生 sticky session。

    家里那个代理没有凭据，也没有 ``-数字`` 结尾的 session 段——必须原样透传，
    被改坏了就连不上，而症状只是「连接被拒绝」，不好一眼看出根因。
    """

    @pytest.mark.parametrize("source", ["holland2stay", "xior", "ourdomain"])
    def test_unchanged_for_every_source(self, source):
        assert config._with_source_session(HOME, source) == HOME

    def test_unchanged_when_rotating(self):
        assert config._with_source_session(HOME, "ourdomain", True) == HOME

    def test_commercial_ones_still_get_their_session(self):
        """对照：有 session 段的仍要被改写，否则各 source 挤同一个出口 IP。"""
        got = config._with_source_session(P1, "xior")
        assert got != P1 and "u1-nl-" in got

    def test_each_source_gets_a_distinct_session(self):
        a = config._with_source_session(P1, "xior")
        b = config._with_source_session(P1, "holland2stay")
        assert a != b


class TestDegradeOnlyAfterHomeAlsoFails:
    """三个都挂了才降级直连原生 IP——那时 _apply_proxyless_gate 才该生效。"""

    def test_no_native_fallback_while_home_is_available(self, pool):
        _cool(P1)
        _cool(P2)
        assert config.get_proxy_url() == HOME
        assert not config.is_proxy_native_fallback_active()

    def test_native_fallback_once_all_three_are_cooling(self, pool):
        for p in (P1, P2, HOME):
            _cool(p)
        assert config.get_proxy_url() == ""
        assert config.is_proxy_native_fallback_active()
