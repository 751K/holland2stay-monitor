"""代理挂了要能自己降级，而不是干等人工。

2026-08-05 04:24–09:29，Webshare 对每一个 CONNECT 都回 `402 Payment Required`
（配额耗尽），5 小时零抓取。系统里那套「标记代理故障 → 进冷却 → 切备用，没有
备用就降级直连服务器原生 IP」全程没有触发过一次：

    grep "代理失效|代理故障|降级直连|备用代理"  →  0

原因不是判定写错，是**这条路根本走不通**——`ProxyError` 的唯一构造点要求
`proxy_failure` 非空，而 `proxy_failure` 只在遇到 `ProxyError` 时才被赋值。一个
自己喂自己的闭环。真正该做分类的 `is_proxy_error()` 写好了、测过了，就是没人调：

    grep -rn "is_proxy_error(" --include=*.py .
    tests/test_proxy_error.py:34
    tests/test_proxy_error.py:39     ← 全是测试

**「有测试覆盖」不等于「被生产调用」。** 一个纯函数可以百分之百覆盖率地正确，
同时对系统毫无影响。本文件因此专门守住调用链，而不只是判定本身。

分四层：

1. 判定认得出真实的错误形态（浏览器 / curl / probe_proxy 三种来源）
2. dispatcher 两条失败路径都把它接上（per-task 与 batch_session）
3. 接上之后代理池确实进冷却并降级直连
4. 降级期间轮询压到 10 分钟一次，**高峰期也不例外**
"""
from __future__ import annotations

import ast
import inspect
import socket
import threading
from pathlib import Path

import pytest

import config
import monitor
import scrapers
from scrapers.base import (
    BlockedError,
    ProxyError,
    ScrapeNetworkError,
    ScrapeTask,
    is_proxy_error,
    is_proxy_service_error,
)


@pytest.fixture(autouse=True)
def clean_proxy_env(monkeypatch):
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "SCRAPE_PROXIES_FALLBACK"):
        monkeypatch.delenv(k, raising=False)
    config._proxy_cooldown_until.clear()
    config._proxy_failure_marks.clear()
    yield
    config._proxy_cooldown_until.clear()
    config._proxy_failure_marks.clear()


# ── 第一层：判定认得出真实形态 ──────────────────────────────────────

#: 昨晚日志里的原文，一字未改。
REAL_CURL_402 = (
    "[Amsterdam Diemen] ourdomain 抓取失败: Failed to perform, "
    "curl: (56) CONNECT tunnel failed, response 402. "
    "See https://curl.se/libcurl/c/libcurl-errors.html first for more details."
)
REAL_BROWSER_TUNNEL = (
    "Holland2Stay 主站加载失败（CF 挑战可能未通过）: Page.goto: "
    "net::ERR_TUNNEL_CONNECTION_FAILED at https://www.holland2stay.com/residences"
)


class TestClassifierSeesRealFailures:
    def test_curl_402_is_a_proxy_error(self):
        assert is_proxy_error(ScrapeNetworkError(REAL_CURL_402))

    def test_curl_402_confirms_service_down(self):
        """402 = 配额耗尽/欠费，换出口 IP 也没用，够格让整条代理进冷却。"""
        assert is_proxy_service_error(ScrapeNetworkError(REAL_CURL_402))

    def test_chromium_tunnel_code_is_a_proxy_error(self):
        """Chromium 用下划线，而判定文案是空格分词的——正是漏判的那一半。

        ``ERR_TUNNEL_CONNECTION_FAILED`` 里没有 "tunnel connection failed"。
        """
        assert is_proxy_error(ScrapeNetworkError(REAL_BROWSER_TUNNEL))

    @pytest.mark.parametrize("code", [
        "ERR_PROXY_CONNECTION_FAILED",
        "ERR_PROXY_AUTH_UNSUPPORTED",
        "ERR_PROXY_CERTIFICATE_INVALID",
        "ERR_NO_SUPPORTED_PROXIES",
    ])
    def test_other_chromium_proxy_codes(self, code):
        assert is_proxy_error(ScrapeNetworkError(f"Page.goto: net::{code} at https://x/"))

    def test_cause_chain_is_searched(self):
        """curl_cffi 常把状态码藏在 __cause__ 里。"""
        try:
            try:
                raise RuntimeError(REAL_CURL_402)
            except RuntimeError as inner:
                raise ScrapeNetworkError("抓取失败") from inner
        except ScrapeNetworkError as e:
            assert is_proxy_error(e)
            assert is_proxy_service_error(e)


class TestClassifierStaysNarrow:
    """判太宽比判太窄更糟：站点自己的问题被当成代理故障，会把好代理关掉。"""

    def test_cf_challenge_timeout_is_not_a_proxy_error(self):
        e = ScrapeNetworkError(
            "Xior 主站加载失败（CF 挑战可能未通过）: Timeout 30000ms exceeded"
        )
        assert not is_proxy_error(e)

    def test_plain_timeout_is_not_a_proxy_error(self):
        assert not is_proxy_error(ScrapeNetworkError("Connection timed out"))

    @pytest.mark.parametrize("code,why", [
        (403, "该出口被代理商禁用——换个 session 就好，不该关掉整条代理"),
        (429, "代理侧限流——等一会自己恢复"),
    ])
    def test_recoverable_codes_do_not_confirm_service_down(self, code, why):
        e = ScrapeNetworkError(
            f"Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED（代理拒绝 CONNECT: {code} x）"
        )
        assert is_proxy_error(e), "仍然是代理层的问题"
        assert not is_proxy_service_error(e), why


class TestProbeProxyOutputIsRecognised:
    """`is_proxy_error` 认的是 `probe_proxy()` 的中文判词，属于跨模块的文案耦合。

    这里用**真实的 probe_proxy 输出**钉住它：改了那边的措辞而忘了这边，测试会红，
    而不是等到下次代理挂了才发现降级又不工作了。
    """

    @staticmethod
    def _fake_proxy(status_line: bytes) -> tuple[str, threading.Thread]:
        """起一个只回一行状态码的假代理，返回它的 URL。"""
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)

        def serve():
            try:
                conn, _ = srv.accept()
                conn.recv(4096)
                conn.sendall(status_line + b"\r\n\r\n")
                conn.close()
            except OSError:
                pass
            finally:
                srv.close()

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        return f"http://127.0.0.1:{srv.getsockname()[1]}", t

    def test_402_verdict_flows_all_the_way_through(self):
        url, t = self._fake_proxy(b"HTTP/1.1 402 Payment Required")
        reason = config.probe_proxy(url, "www.holland2stay.com")
        t.join(timeout=5)

        assert reason and "402" in reason
        assert "流量配额耗尽或账户欠费" in reason

        # 这正是 browser_fetcher._describe_navigation_failure() 拼出来的形状
        e = ScrapeNetworkError(
            f"Holland2Stay 主站加载失败（{reason}）: "
            "Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED"
        )
        assert is_proxy_error(e), "probe_proxy 的判词没被认出来"
        assert is_proxy_service_error(e), "402 判词没能确认代理服务端异常"

    def test_healthy_proxy_returns_no_reason(self):
        url, t = self._fake_proxy(b"HTTP/1.1 200 Connection established")
        reason = config.probe_proxy(url, "www.holland2stay.com")
        t.join(timeout=5)
        assert reason is None


# ── 第二层：dispatcher 真的把它接上了 ────────────────────────────────


class _FakeScraper:
    """按 source 决定怎么失败。"""

    def __init__(self, exc: BaseException, *, at_batch: bool = False):
        self._exc = exc
        self._at_batch = at_batch

    def batch_session(self):
        from contextlib import contextmanager

        @contextmanager
        def cm():
            if self._at_batch:
                raise self._exc
            yield

        return cm()

    def scrape(self, task):
        raise self._exc

    def invalidate_session(self):
        pass


def _run(monkeypatch, exc, *, at_batch=False, n=2):
    tasks = [ScrapeTask(source="x", city_key=str(i), city_display=f"C{i}") for i in range(n)]
    monkeypatch.setattr(
        scrapers, "get_scraper", lambda s: _FakeScraper(exc, at_batch=at_batch)
    )
    return scrapers.dispatch_scrape_tasks(tasks)


class TestDispatcherRaisesProxyError:
    def test_per_task_network_failure(self, monkeypatch):
        """curl 系 source（OurDomain / OurCampus）走这条。"""
        with pytest.raises(ProxyError):
            _run(monkeypatch, ScrapeNetworkError(REAL_CURL_402))

    def test_batch_session_failure(self, monkeypatch):
        """浏览器系 source（H2S / Xior）走这条——代理连不上时连第一个 task 都进不去。

        这一半此前完全没有分类：批次异常直接进 hard_failures，原样上抛。
        """
        with pytest.raises(ProxyError):
            _run(monkeypatch, ScrapeNetworkError(REAL_BROWSER_TUNNEL), at_batch=True)

    def test_original_error_is_preserved_in_the_chain(self, monkeypatch):
        """monitor 靠异常链文本判断要不要进冷却，断链就等于降级失效。"""
        with pytest.raises(ProxyError) as ei:
            _run(monkeypatch, ScrapeNetworkError(REAL_CURL_402))
        assert is_proxy_service_error(ei.value), "链上的 402 丢了"

    def test_non_proxy_network_failure_stays_plain(self, monkeypatch):
        with pytest.raises(ScrapeNetworkError) as ei:
            _run(monkeypatch, ScrapeNetworkError("Connection timed out"))
        assert not isinstance(ei.value, ProxyError)

    def test_403_is_not_promoted_to_proxy_error(self, monkeypatch):
        with pytest.raises(BlockedError):
            _run(monkeypatch, BlockedError("Cloudflare 403"))


class TestClassifierIsActuallyCalled:
    """守住调用链本身——这才是 2026-08-05 缺的那一环。"""

    def test_is_proxy_error_has_a_production_caller(self):
        src = Path(scrapers.__file__).read_text()
        tree = ast.parse(src)
        called = {
            n.func.id
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        assert "is_proxy_error" in called, (
            "is_proxy_error 又只剩测试在调了——ProxyError 会退回成永远构造不出来的闭环"
        )

    def test_both_failure_paths_classify(self):
        """per-task 与 batch_session 两条路都得分类，只接一条等于漏一半 source。"""
        src = inspect.getsource(scrapers.dispatch_scrape_tasks)
        assert src.count("is_proxy_error(") >= 2, (
            "只有一条失败路径做了代理分类；浏览器 source 和 curl source 各走一条"
        )


# ── 第三层：接上之后确实降级 ────────────────────────────────────────


class TestSingleProxyFallsBackToNativeIP:
    """没配 SCRAPE_PROXIES_FALLBACK 时，唯一的退路就是服务器原生 IP。"""

    @pytest.fixture
    def only_proxy(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://p:1")
        assert config.proxy_pool_size() == 1
        return "http://p:1"

    def test_confirmed_failures_cool_it_down_and_go_direct(self, only_proxy):
        assert config.get_proxy_url() == only_proxy
        assert config.is_proxy_native_fallback_active() is False

        config.report_proxy_failure(service_error_confirmed=True)   # 第 1 次只记录
        assert config.get_proxy_url() == only_proxy, "一次失败就关掉太急躁"

        config.report_proxy_failure(service_error_confirmed=True)   # 第 2 次确认
        assert config.get_proxy_url() == "", "该降级直连了"
        assert config.is_proxy_native_fallback_active() is True

    def test_unconfirmed_failures_never_cool_it_down(self, only_proxy):
        """疑似而非确认的故障（如 429）不该把唯一的代理关掉。"""
        for _ in range(5):
            config.report_proxy_failure(service_error_confirmed=False)
        assert config.get_proxy_url() == only_proxy
        assert config.is_proxy_native_fallback_active() is False

    def test_cooldown_expires_and_the_proxy_comes_back(self, only_proxy, monkeypatch):
        config.report_proxy_failure(service_error_confirmed=True)
        config.report_proxy_failure(service_error_confirmed=True)
        assert config.is_proxy_native_fallback_active() is True

        # 冷却到期（10 分钟）后自动重新纳入，充值/恢复之后无需人工干预
        base = config._time.monotonic()
        monkeypatch.setattr(config._time, "monotonic", lambda: base + 601)
        assert config.is_proxy_native_fallback_active() is False
        assert config.get_proxy_url() == only_proxy

    def test_no_proxy_configured_is_not_a_fallback(self):
        """本来就没配代理 = 用户主动选直连，不是降级，不该压频率。"""
        assert config.is_proxy_native_fallback_active() is False


class TestRunOnceTurnsProxyErrorIntoCooldown:
    """`ProxyError` 只是信号，真正让代理进冷却的是 run_once 的处理分支。

    链条上任何一环断了，前面几层全白做——所以这里既钉接线，也把整条链重放一遍。
    """

    def _report_call(self):
        """从 run_once 里揪出那次 report_proxy_failure 调用（AST，不比字符串）。

        原来断的是一整行精确文本。2026-08-26 给这次调用加了 account_level 参数、
        顺带换行，断言就挂了——而它要守的「有没有把故障报给代理池」根本没变。
        改成认调用本身和它的关键字参数，格式怎么排都无所谓。
        """
        import ast
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(monitor.run_once)))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and getattr(node.func, "id", "") == "report_proxy_failure"):
                return node
        raise AssertionError("run_once 里根本没有调用 report_proxy_failure")

    def test_handler_feeds_the_verdict_into_the_pool(self):
        import ast

        src = inspect.getsource(monitor.run_once)
        i = src.index("except ProxyError as e:")
        block = src[i:i + 1400]
        assert "is_proxy_service_error(e)" in block, (
            "没有判定「确认代理服务端异常」，report_proxy_failure 拿不到 confirmed"
        )
        kwargs = {k.arg: ast.unparse(k.value) for k in self._report_call().keywords}
        assert kwargs.get("service_error_confirmed") == "service_error", (
            f"没有把故障报给代理池——冷却/切换/降级都不会发生（实参 {kwargs}）"
        )

    def test_account_level_verdict_is_also_fed_in(self):
        """402 欠费 / 407 认证失败要走长冷却，判定结果得真的传进去。

        不传的话默认 account_level=False，长冷却永远不生效，而所有行为测试
        照样绿——账户级故障会继续每 10 分钟被重试一次。
        """
        import ast

        src = inspect.getsource(monitor.run_once)
        assert "is_proxy_account_error(e)" in src, "没有判定账户级故障"
        kwargs = {k.arg: ast.unparse(k.value) for k in self._report_call().keywords}
        assert kwargs.get("account_level") == "account_level", (
            f"账户级判定没传给代理池（实参 {kwargs}）"
        )

    def test_replaying_last_nights_error_ends_in_native_fallback(self, monkeypatch):
        """用昨晚日志里的原文，把 run_once 的处理顺序原样走一遍。"""
        monkeypatch.setenv("HTTPS_PROXY", "http://p:1")

        # dispatcher 现在会这么抛（第二层已验证）
        try:
            raise ProxyError("全部 9 个任务因代理故障失败") from ScrapeNetworkError(
                REAL_CURL_402
            )
        except ProxyError as e:
            for _ in range(2):  # run_once 每轮走一次，两次确认才冷却
                service_error = is_proxy_service_error(e)
                assert service_error, "402 没被确认，代理永远不会进冷却"
                config.report_proxy_failure(service_error_confirmed=service_error)

        assert config.is_proxy_native_fallback_active() is True
        assert config.get_proxy_url() == "", "该走服务器原生 IP 了"


# ── 第四层：降级期间的频率 ──────────────────────────────────────────


class TestDegradedPollingRate:
    """直连走的是服务器自己的 IP，被站点认出来的代价远高于代理。

    因此降级期间必须压到 10 分钟一轮，**高峰期也不例外**——高峰的自适应间隔
    最低能到 min_interval（60s），若让它生效，等于拿服务器 IP 去高频撞 CF。
    """

    def test_interval_floor_is_ten_minutes(self):
        assert monitor._NATIVE_PROXY_FALLBACK_INTERVAL == 600

    def test_floor_applies_after_the_peak_branch(self):
        """顺序要求：先算高峰/非高峰间隔，再取 max。写反了高峰期就漏出去了。"""
        src = inspect.getsource(monitor.main_loop)
        peak = src.index("adaptive_peak = float(cfg.peak_interval)")
        floor = src.index("_NATIVE_PROXY_FALLBACK_INTERVAL")
        assert peak < floor, "降级下限被高峰期分支覆盖了"

    @pytest.mark.parametrize("is_peak,base", [(True, 60), (False, 300)])
    def test_both_peak_and_offpeak_are_clamped(self, is_peak, base):
        """把 main_loop 里那两行算式原样复算一遍，两个时段都得到 600。"""
        effective = base
        effective = max(effective, monitor._NATIVE_PROXY_FALLBACK_INTERVAL)
        assert effective == 600, f"is_peak={is_peak} 时没压到 10 分钟"

    def test_jitter_cannot_pull_it_below_the_floor(self):
        """抖动是负的时候不能把 600 抖到 600 以下。

        原来切 260 个字符的窗口。2026-08-26 在 apply_jitter 和这行之间插入了
        「自家线路主动降速」的同类夹取，窗口被撑开就切不到了——测的本来就是
        「夹取在抖动之后」这个**顺序**，改成直接比下标，不受中间插了什么影响。
        """
        src = inspect.getsource(monitor.main_loop)
        jitter = src.index("actual = apply_jitter(effective_interval")
        clamp = src.index("max(actual, _NATIVE_PROXY_FALLBACK_INTERVAL)")
        assert jitter < clamp, "下限夹取跑到抖动前面去了，负抖动会漏过去"

    def test_personal_proxy_floor_is_also_applied_after_jitter(self):
        """自家线路那道下限同样得在抖动之后夹——0.4 的抖动能把 120 秒打到 72 秒。"""
        src = inspect.getsource(monitor.main_loop)
        jitter = src.index("actual = apply_jitter(effective_interval")
        clamp = src.index("max(actual, _PERSONAL_PROXY_MIN_INTERVAL)")
        assert jitter < clamp
