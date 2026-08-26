"""代理全挂时，「降级直连」必须真的直连，而且只跑直连打得通的 source。

2026-08-26 生产实况
-------------------
webshare 两个账号（``qrrsiysu-nl-222900`` / ``fdtlrlbo-nl-790346``）同时返回
402，代理池整体归零。代码里那条「降级为服务器原生 IP 直连」触发了五次，
**一轮都没抓成**，全站两小时十分钟零房源。

事后逐条量出来两个独立的 bug，各挡住一半：

1. **浏览器**：``proxy=None`` 关不掉 Chromium 自己的代理解析，它照读
   ``HTTP_PROXY`` / ``HTTPS_PROXY``，继续拨那个死代理。指纹是代码自己打出的
   矛盾日志——「代理层报错，但本次并未走代理」。加 ``--no-proxy-server``
   之后失败时间从 3 秒变 275 秒，因为浏览器终于真的连上 CF 在解挑战了。
2. **curl_cffi**：``proxies={}`` 静默回落到环境变量。并排实测同一个 URL：
   ``{"http":"","https":""}`` → 200，``{}`` → 402。同一个坑 ``net.py`` 的模块
   注释里已经写过一次（2026-08-24），当时只修了非抓取路径。

第三件事是分级：H2S / Xior 在机房 IP 上被 Cloudflare 硬挡（实测挑战 90 秒
未解开 ×3），它们的必然失败会让 ``dispatch_scrape_tasks`` 判定「全员失败」，
把同一轮里本来能成的 OurDomain / OurCampus 一起掀掉。
"""
from __future__ import annotations

import pytest

import monitor
from monitor import _PROXYLESS_CAPABLE_SOURCES, _apply_proxyless_gate


class _T:
    def __init__(self, source, city="X"):
        self.source = source
        self.city_key = city
        self.city_display = city


@pytest.fixture
def degraded(monkeypatch):
    monkeypatch.setattr(
        "config.is_proxy_native_fallback_active", lambda: True, raising=False)


@pytest.fixture
def healthy(monkeypatch):
    monkeypatch.setattr(
        "config.is_proxy_native_fallback_active", lambda: False, raising=False)


class TestProxylessGate:
    def test_no_gate_while_proxy_is_healthy(self, healthy):
        """代理正常时这道闸门必须完全透明，一个任务都不能少。"""
        tasks = [_T("holland2stay"), _T("xior"), _T("ourdomain")]
        assert _apply_proxyless_gate(tasks) == tasks

    def test_drops_only_what_cannot_work_direct(self, degraded):
        tasks = [_T("holland2stay"), _T("xior"), _T("ourdomain"), _T("ourcampus")]
        kept = _apply_proxyless_gate(tasks)
        assert {t.source for t in kept} == {"ourdomain", "ourcampus"}

    def test_the_survivors_are_the_measured_ones(self):
        """名单是 2026-08-26 在服务器上实测出来的，不是猜的。

        改这张表意味着推翻那次测量——真要改，先重测一遍。
        """
        assert _PROXYLESS_CAPABLE_SOURCES == frozenset({"ourdomain", "ourcampus"})

    def test_h2s_and_xior_are_not_in_it(self):
        """两家都被 CF 硬挡（挑战 90 秒未解开 ×3），放进来等于每轮制造必然失败。"""
        assert "holland2stay" not in _PROXYLESS_CAPABLE_SOURCES
        assert "xior" not in _PROXYLESS_CAPABLE_SOURCES

    def test_all_dropped_yields_empty_not_original(self, degraded):
        """一个都不剩时要返回空，不能「保险起见」把原列表退回去。

        退回去就等于闸门不存在，正是 08-26 那五轮的行为。
        """
        assert _apply_proxyless_gate([_T("holland2stay"), _T("xior")]) == []

    def test_unknown_source_is_dropped(self, degraded):
        """没登记过的 source 按打不通处理——没量过就不该假设它能直连。"""
        assert _apply_proxyless_gate([_T("brand_new_platform")]) == []

    def test_state_check_failure_does_not_stop_scraping(self, monkeypatch):
        """判不出代理状态时宁可让它去试一轮，也不能把 source 静默停掉。"""
        def _boom():
            raise RuntimeError("meta 读失败")
        monkeypatch.setattr(
            "config.is_proxy_native_fallback_active", _boom, raising=False)
        tasks = [_T("holland2stay")]
        assert _apply_proxyless_gate(tasks) == tasks

    def test_logs_what_it_dropped(self, degraded, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="monitor"):
            _apply_proxyless_gate([_T("holland2stay"), _T("ourdomain")])
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "holland2stay" in msgs
        assert "ourdomain" in msgs, "没说清楚还有谁在抓"

    def test_gate_runs_before_sharding(self):
        """顺序要紧：被代理挡住的 source 不该白白推进分片游标。"""
        import inspect
        src = inspect.getsource(monitor.run_once)
        assert src.index("_apply_proxyless_gate") < src.index("_apply_task_sharding")


class TestCurlDoesNotFallBackToEnv:
    """``proxies={}`` 会静默回落到环境变量——两处 scraper 都不许再这么写。"""

    @pytest.mark.parametrize("module", [
        "scrapers/ourdomain.py", "scrapers/xior.py", "tools/doctor.py"])
    def test_empty_dict_is_gone(self, module):
        from pathlib import Path
        src = Path(module).read_text(encoding="utf-8")
        assert "if proxy else {}" not in src, (
            f"{module} 又写回 proxies={{}} 了——空字典会回落到环境变量，"
            "「降级直连」就再也不会真的直连")

    @pytest.mark.parametrize("module", [
        "scrapers/ourdomain.py", "scrapers/xior.py", "tools/doctor.py"])
    def test_uses_the_shared_constant(self, module):
        from pathlib import Path
        src = Path(module).read_text(encoding="utf-8")
        assert "NO_PROXY_CURL" in src

    def test_the_constant_is_explicit_empty_strings(self):
        """不是 {}、不是 None、不是 trust_env=False——只有空串真的关掉代理。"""
        from net import NO_PROXY_CURL
        assert NO_PROXY_CURL == {"http": "", "https": ""}


class TestBrowserDoesNotFallBackToEnv:
    def _args(self, monkeypatch, *, proxy: str, env: dict) -> list[str]:
        """跑一遍 _launch 的参数拼装，拿到最终交给 Chromium 的 args。"""
        import browser_fetcher as bf

        captured: dict = {}

        def _fake_open(self, chromium_args, proxy_url):
            captured["args"] = list(chromium_args)
            raise RuntimeError("stop here")  # 不真开浏览器

        monkeypatch.setattr(bf.BrowserFetcher, "_open_browser", _fake_open)
        monkeypatch.setattr(bf, "get_proxy_url", lambda *a, **k: proxy, raising=False)
        monkeypatch.setattr("config.get_proxy_url", lambda *a, **k: proxy, raising=False)
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                  "http_proxy", "https_proxy", "all_proxy"):
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)

        f = bf.BrowserFetcher(headless=True, profile=bf.H2S_PROFILE)
        with pytest.raises(RuntimeError):
            f._launch()
        return captured.get("args", [])

    def test_degrading_with_env_proxy_blocks_chromium(self, monkeypatch):
        """这是 08-26 那个 bug 的核心：没有它，Chromium 照拨死代理。"""
        args = self._args(monkeypatch, proxy="", env={"HTTPS_PROXY": "http://dead:80"})
        assert "--no-proxy-server" in args

    def test_lowercase_env_also_counts(self, monkeypatch):
        """curl 系工具认小写，Chromium 也认——只查大写会漏。"""
        args = self._args(monkeypatch, proxy="", env={"https_proxy": "http://dead:80"})
        assert "--no-proxy-server" in args

    def test_not_added_when_using_a_proxy(self, monkeypatch):
        """正常走代理时加这个 flag 会把代理关掉，等于自断出口。"""
        args = self._args(monkeypatch, proxy="http://live:80",
                          env={"HTTPS_PROXY": "http://live:80"})
        assert "--no-proxy-server" not in args

    def test_not_added_when_no_proxy_configured_at_all(self, monkeypatch):
        """没配过代理的自部署用户本来就没 env 可漏，别平白多个变量。"""
        args = self._args(monkeypatch, proxy="", env={})
        assert "--no-proxy-server" not in args
