"""账户级代理故障（402 欠费 / 407 认证失败）走长冷却。

起因（2026-08-26 生产）
----------------------
webshare 两个账号同时 402，代理池归零。冷却统一 ``_PROXY_COOLDOWN_SEC=600``，
10 分钟一到就把死代理放回候选，于是每小时有六段窗口各 source 重新去敲一个明知
欠费的代理。同一次故障在三个 source 上表现完全不同：

    holland2stay   浏览器一建用 2 小时（_BROWSER_MAX_AGE）  → 一直在好线路上，没事
    xior           浏览器 15 分钟重建                        → 重建撞上冷却到期就中招
    ourcampus      curl，每轮现取                            → 每轮都可能中招

实测遥测：09:34 两家都 ok，09:39 ourcampus 挂，09:44 起两家全挂——而 H2S 全程正常。

判据为什么只认 402/407
----------------------
判错的代价不对称：把瞬时故障误判成账户级，会让一个其实已经恢复的代理白等一小时；
反过来只是回到现状（10 分钟）。所以宁可漏判，只认明确的状态码，不并入 provider
自己的错误头——那些既可能是账户问题也可能是网关问题，从字符串里分不出来。
"""
from __future__ import annotations

import pytest

import config
from scrapers.base import (
    ProxyError,
    is_proxy_account_error,
    is_proxy_service_error,
)

P1 = "http://u1-nl-111111:pw@p.webshare.io:80"
P2 = "http://u2-nl-222222:pw@p.webshare.io:80"


def _exc(text: str) -> Exception:
    return ProxyError(text)


class TestClassification:
    @pytest.mark.parametrize("text", [
        "Failed to perform, curl: (56) CONNECT tunnel failed, response 402.",
        "代理拒绝 CONNECT: 402 Payment Required（流量配额耗尽或账户欠费）",
        "CONNECT tunnel failed, response 407",
    ])
    def test_account_level(self, text):
        assert is_proxy_account_error(_exc(text))

    @pytest.mark.parametrize("text", [
        "CONNECT tunnel failed, response 502",
        "代理拒绝 CONNECT: 503 Service Unavailable",
    ])
    def test_transient_is_not_account_level(self, text):
        """502/503 可能几分钟后自己好，不该被判长冷却。"""
        assert not is_proxy_account_error(_exc(text))
        assert is_proxy_service_error(_exc(text)), "但它仍该够格进冷却"

    @pytest.mark.parametrize("text", [
        "Connection reset by peer", "timed out", "SSL handshake failure", "",
    ])
    def test_plain_network_noise_is_neither(self, text):
        assert not is_proxy_account_error(_exc(text))

    def test_provider_markers_alone_do_not_imply_account_level(self):
        """X-Webshare-* 既可能是账户问题也可能是网关问题——分不出来就别猜。"""
        e = _exc("x-webshare-error: internal_error_auth_circuit_breaker_open 502")
        assert is_proxy_service_error(e)
        assert not is_proxy_account_error(e)

    def test_both_codes_still_count_as_service_error(self):
        """长冷却是在「够格进冷却」之上的更窄一档，不能把 402 挤出冷却资格。"""
        for code in (402, 407):
            assert is_proxy_service_error(_exc(f"response {code}"))


class TestCooldownLength:
    @pytest.fixture(autouse=True)
    def _pool(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", P1)
        monkeypatch.delenv("HTTP_PROXY", raising=False)
        monkeypatch.delenv("ALL_PROXY", raising=False)
        monkeypatch.setenv("SCRAPE_PROXIES_FALLBACK", P2)
        config._proxy_cooldown_until.clear()
        config._proxy_failure_marks.clear()
        yield
        config._proxy_cooldown_until.clear()
        config._proxy_failure_marks.clear()

    def _cool_it(self, *, account_level: bool) -> float:
        """连续报到确认阈值，返回该代理的冷却时长（秒）。"""
        import time
        for _ in range(config._PROXY_FAILURE_CONFIRM_THRESHOLD):
            config.report_proxy_failure(
                P1, service_error_confirmed=True, account_level=account_level)
        until = config._proxy_cooldown_until.get(P1)
        assert until is not None, "根本没进冷却"
        return until - time.monotonic()

    def test_transient_keeps_ten_minutes(self):
        assert 500 < self._cool_it(account_level=False) <= 600

    def test_account_level_gets_the_long_one(self):
        got = self._cool_it(account_level=True)
        assert got > 600, "账户级还是只冷却 10 分钟，等于没改"
        assert 3500 < got <= 3600

    def test_default_is_one_hour(self):
        assert config._PROXY_ACCOUNT_COOLDOWN_SEC == 3600

    def test_long_cooldown_is_longer_than_the_short_one(self):
        """这条是不变量，不是数值——将来两个默认值怎么调都得保持这个关系。"""
        assert config._PROXY_ACCOUNT_COOLDOWN_SEC > config._PROXY_COOLDOWN_SEC

    def test_still_falls_through_to_the_next_proxy(self):
        """长冷却只是别再敲它，不是卡住整条链——下一个代理要立刻顶上。"""
        self._cool_it(account_level=True)
        assert config.get_proxy_url() == P2

    def test_not_cooled_when_not_confirmed(self):
        """未确认是代理服务端异常时，长短都不该进冷却。"""
        for _ in range(config._PROXY_FAILURE_CONFIRM_THRESHOLD):
            config.report_proxy_failure(
                P1, service_error_confirmed=False, account_level=True)
        assert P1 not in config._proxy_cooldown_until


class TestOverride:
    @pytest.mark.parametrize("raw,want", [("7200", 7200), ("60", 60)])
    def test_env_override(self, monkeypatch, raw, want):
        monkeypatch.setenv("PROXY_ACCOUNT_COOLDOWN_SEC", raw)
        assert config._env_int_positive("PROXY_ACCOUNT_COOLDOWN_SEC", 3600) == want

    @pytest.mark.parametrize("raw", ["", "abc", "0", "-1"])
    def test_bad_values_fall_back(self, monkeypatch, raw):
        """手滑写错不该让进程起不来，回落到默认（冷却更久 = 少骚扰已知故障的代理）。"""
        monkeypatch.setenv("PROXY_ACCOUNT_COOLDOWN_SEC", raw)
        assert config._env_int_positive("PROXY_ACCOUNT_COOLDOWN_SEC", 3600) == 3600


class TestRestartIsTheEscapeHatch:
    @pytest.fixture(autouse=True)
    def _pool(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", P1)
        monkeypatch.delenv("HTTP_PROXY", raising=False)
        monkeypatch.delenv("ALL_PROXY", raising=False)
        monkeypatch.delenv("SCRAPE_PROXIES_FALLBACK", raising=False)
        config._proxy_cooldown_until.clear()
        yield
        config._proxy_cooldown_until.clear()

    def test_cooldown_state_is_process_level(self):
        """充完值想立刻恢复就重启——冷却存在进程级 dict 里，重启即清零。

        这是长冷却的安全阀：判长了也有一条不用等的出路。文档里写了这条
        （.env.example），这里钉住它确实成立。
        """
        assert isinstance(config._proxy_cooldown_until, dict)
        config._proxy_cooldown_until[P1] = 1e18
        config._proxy_cooldown_until.clear()          # ≈ 进程重启
        assert config.get_proxy_url() == P1
