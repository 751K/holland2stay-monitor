"""
代理池 + 故障切换测试（config.get_proxy_url / report_proxy_failure）。

主代理挂了（webshare 502）自动切到 SCRAPE_PROXIES_FALLBACK 里的备用，
连续确认故障的代理进 10 min 冷却，冷却结束自动重新纳入；若所有代理都在
冷却，抓取层降级为服务器原生 IP 直连，monitor 再把轮询频率压到最多
10 min 一次。
"""
from __future__ import annotations

import pytest

import config
from scrapers.base import ProxyError, is_proxy_account_error, is_proxy_service_error


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


class TestRotatingSession:
    """OurDomain 需要的是**换 IP**，不是固定 IP。

    它没有 clearance 可复用，抗封手段是轮换 TLS 指纹——而那只在换 IP 的前提下
    才有效。2026-08-02 实测：固定到专属 sticky IP 后，同一个 IP 被 CF 盯上时
    四个指纹轮完仍然全是 403，无法自愈。
    """

    STICKY = "http://acct-nl-790346:pw@p.webshare.io:80"

    def test_rotating_changes_every_call(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", self.STICKY)
        seen = {config.get_proxy_url("ourdomain", rotating=True) for _ in range(5)}
        assert len(seen) == 5, seen

    def test_non_rotating_stays_stable(self, monkeypatch):
        """默认仍是稳定的——H2S / Xior 靠它复用 clearance。"""
        monkeypatch.setenv("HTTPS_PROXY", self.STICKY)
        seen = {config.get_proxy_url("holland2stay") for _ in range(5)}
        assert len(seen) == 1

    def test_rotating_preserves_account_and_country(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", self.STICKY)
        url = config.get_proxy_url("ourdomain", rotating=True)
        assert url.startswith("http://acct-nl-")
        assert ":pw@p.webshare.io:80" in url

    def test_rotating_respects_unrecognised_shapes(self, monkeypatch):
        """非 sticky 形态照样不改写。"""
        monkeypatch.setenv("HTTPS_PROXY", "http://acct-us-rotate:pw@p.webshare.io:80")
        assert config.get_proxy_url("ourdomain", rotating=True) == \
            "http://acct-us-rotate:pw@p.webshare.io:80"


# ── 账户级代理故障走长冷却（原 test_proxy_account_cooldown.py） ─────
#
# 起因（2026-08-26 生产）
# ----------------------
# webshare 两个账号同时 402，代理池归零。冷却统一 ``_PROXY_COOLDOWN_SEC=600``，
# 10 分钟一到就把死代理放回候选，于是每小时有六段窗口各 source 重新去敲一个明知
# 欠费的代理。同一次故障在三个 source 上表现完全不同：
#
#     holland2stay   浏览器一建用 2 小时（_BROWSER_MAX_AGE）  → 一直在好线路上，没事
#     xior           浏览器 15 分钟重建                        → 重建撞上冷却到期就中招
#     ourcampus      curl，每轮现取                            → 每轮都可能中招
#
# 实测遥测：09:34 两家都 ok，09:39 ourcampus 挂，09:44 起两家全挂——而 H2S 全程正常。
#
# 判据为什么只认 402/407
# ----------------------
# 判错的代价不对称：把瞬时故障误判成账户级，会让一个其实已经恢复的代理白等一小时；
# 反过来只是回到现状（10 分钟）。所以宁可漏判，只认明确的状态码，不并入 provider
# 自己的错误头——那些既可能是账户问题也可能是网关问题，从字符串里分不出来。

ACCT_P1 = "http://u1-nl-111111:pw@p.webshare.io:80"
ACCT_P2 = "http://u2-nl-222222:pw@p.webshare.io:80"


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
        monkeypatch.setenv("HTTPS_PROXY", ACCT_P1)
        monkeypatch.delenv("HTTP_PROXY", raising=False)
        monkeypatch.delenv("ALL_PROXY", raising=False)
        monkeypatch.setenv("SCRAPE_PROXIES_FALLBACK", ACCT_P2)
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
                ACCT_P1, service_error_confirmed=True, account_level=account_level)
        until = config._proxy_cooldown_until.get(ACCT_P1)
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
        assert config.get_proxy_url() == ACCT_P2

    def test_not_cooled_when_not_confirmed(self):
        """未确认是代理服务端异常时，长短都不该进冷却。"""
        for _ in range(config._PROXY_FAILURE_CONFIRM_THRESHOLD):
            config.report_proxy_failure(
                ACCT_P1, service_error_confirmed=False, account_level=True)
        assert ACCT_P1 not in config._proxy_cooldown_until


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
        monkeypatch.setenv("HTTPS_PROXY", ACCT_P1)
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
        config._proxy_cooldown_until[ACCT_P1] = 1e18
        config._proxy_cooldown_until.clear()          # ≈ 进程重启
        assert config.get_proxy_url() == ACCT_P1


# ── 家庭 IP 只在两个商业代理都失效之后才启用（原 test_home_proxy_is_last_resort.py） ──
#
# 背景（2026-08-26）
# ------------------
# webshare 的两个账号（主 + 备）同时返回 402，代理池整体归零，全站停摆两小时。
# 补救方案是把一台家里常开的机器接成第三个代理——它的出口是自家住宅宽带，
# Cloudflare 对它的评分和商业住宅池同级，H2S / Xior 因此能过。
#
# 但它**必须排在最后**，理由不是技术而是取舍：那是用户自己的家庭宽带，
# 出现在平台访问日志里的是他家的 IP。商业代理还能用的时候就不该动用它。
#
# 本文件钉的就是这个顺序。实现上不需要新代码——``_proxy_pool()`` 保序、
# ``get_proxy_url()`` 取第一个不在冷却的，天然满足；但「天然满足」这件事
# 必须有测试，否则哪天有人给池子加个排序或去重，这条约束会静默消失。

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
