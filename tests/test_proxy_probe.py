"""代理失败要说出真实原因，而不是一律甩给 Cloudflare。

2026-08-05 线上事故：代理账户欠费，CONNECT 一律回 402。Chromium 把它压成
``ERR_TUNNEL_CONNECTION_FAILED``，日志里六百多行全写着「CF 挑战可能未通过」，
排查方向被整整带偏。真相是在容器里手工发了一次 CONNECT 才看到的。

``probe_proxy`` 就是把那次手工操作固化下来：直接问代理要状态码。
"""
from __future__ import annotations

import socket
import threading

import pytest

from config import probe_proxy


class _FakeProxy:
    """只回一行状态行就关闭连接的假代理。"""

    def __init__(self, status_line: str):
        self._status_line = status_line
        self.requests: list[bytes] = []
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(5)
            try:
                self.requests.append(conn.recv(1024))
                conn.sendall(f"{self._status_line}\r\n\r\n".encode())
            except OSError:
                pass

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass


@pytest.fixture
def fake_proxy():
    made: list[_FakeProxy] = []

    def _make(status_line: str) -> _FakeProxy:
        p = _FakeProxy(status_line)
        made.append(p)
        return p

    yield _make
    for p in made:
        p.close()


class TestReadsRealStatus:
    def test_200_means_healthy(self, fake_proxy):
        p = fake_proxy("HTTP/1.1 200 Connection established")
        assert probe_proxy(f"http://127.0.0.1:{p.port}", "example.com") is None

    def test_402_names_the_quota(self, fake_proxy):
        """线上就是这一条。402 必须指向配额/欠费，不能只回一个数字。"""
        p = fake_proxy("HTTP/1.1 402 Payment Required")
        reason = probe_proxy(f"http://127.0.0.1:{p.port}", "example.com")

        assert reason is not None
        assert "402" in reason
        assert "欠费" in reason or "配额" in reason
        # 说的必须是代理，不能让人以为是 Cloudflare
        assert "代理" in reason

    def test_407_names_the_credentials(self, fake_proxy):
        p = fake_proxy("HTTP/1.1 407 Proxy Authentication Required")
        reason = probe_proxy(f"http://127.0.0.1:{p.port}", "example.com")
        assert reason and "407" in reason and "认证" in reason

    def test_unknown_code_still_reported(self, fake_proxy):
        """没收录的状态码也要透出来，不能因为查不到含义就吞掉。"""
        p = fake_proxy("HTTP/1.1 418 I am a teapot")
        reason = probe_proxy(f"http://127.0.0.1:{p.port}", "example.com")
        assert reason and "418" in reason


class TestProbeItselfFails:
    def test_dead_proxy_reports_unreachable(self):
        # 绑一个端口再立刻释放，得到一个几乎必然没人监听的号
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()

        reason = probe_proxy(f"http://127.0.0.1:{port}", "example.com", timeout=2.0)
        assert reason and "连不上代理" in reason

    def test_no_proxy_configured_is_not_a_failure(self):
        assert probe_proxy("", "example.com") is None

    def test_garbage_response_is_reported_not_raised(self, fake_proxy):
        p = fake_proxy("NOT-HTTP whatever")
        reason = probe_proxy(f"http://127.0.0.1:{p.port}", "example.com")
        assert reason and "无法解析" in reason


class TestNavigationFailureMessage:
    """探针的结论要真的出现在抓取失败的那条日志里，否则等于没修。"""

    def _fetcher(self, proxy_url: str = "http://user:pw@proxy.example:80"):
        from browser_fetcher import H2S_PROFILE, BrowserFetcher

        f = BrowserFetcher(profile=H2S_PROFILE)
        f._proxy_url = proxy_url
        return f

    def test_tunnel_error_reports_the_proxy_status(self, monkeypatch):
        import config

        monkeypatch.setattr(
            config, "probe_proxy",
            lambda *a, **kw: "代理拒绝 CONNECT: 402 Payment Required（流量配额耗尽或账户欠费）",
        )
        msg = self._fetcher()._describe_navigation_failure(
            Exception("Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED at https://x/")
        )

        assert "402" in msg and "欠费" in msg
        assert "CF 挑战可能未通过" not in msg, "代理故障还在往 Cloudflare 上引"

    def test_non_proxy_error_keeps_the_cf_wording(self, monkeypatch):
        """超时、DNS 之类和代理无关的失败，原来的说法仍然成立。"""
        import config

        monkeypatch.setattr(
            config, "probe_proxy",
            lambda *a, **kw: pytest.fail("不该为非代理错误去探代理"),
        )
        msg = self._fetcher()._describe_navigation_failure(
            Exception("Page.goto: Timeout 30000ms exceeded")
        )
        assert "CF 挑战可能未通过" in msg

    def test_healthy_proxy_points_elsewhere(self, monkeypatch):
        import config

        monkeypatch.setattr(config, "probe_proxy", lambda *a, **kw: None)
        msg = self._fetcher()._describe_navigation_failure(
            Exception("net::ERR_TUNNEL_CONNECTION_FAILED")
        )
        assert "代理本身可用" in msg

    def test_probe_crash_does_not_swallow_the_original_error(self, monkeypatch):
        import config

        def _boom(*a, **kw):
            raise RuntimeError("探测炸了")

        monkeypatch.setattr(config, "probe_proxy", _boom)
        msg = self._fetcher()._describe_navigation_failure(
            Exception("net::ERR_TUNNEL_CONNECTION_FAILED")
        )
        assert "ERR_TUNNEL_CONNECTION_FAILED" in msg

    def test_probes_the_exit_the_browser_actually_used(self, monkeypatch):
        """rotating profile 上重取 get_proxy_url 会拿到别的 session，
        探到的是另一个出口 IP，结论无效。"""
        import config

        seen: list[str] = []
        monkeypatch.setattr(
            config, "probe_proxy",
            lambda url, host, port=443, **kw: seen.append((url, host, port)) or None,
        )
        f = self._fetcher("http://user:pw@exit-in-use.example:80")
        f._describe_navigation_failure(Exception("net::ERR_TUNNEL_CONNECTION_FAILED"))

        assert seen[0][0] == "http://user:pw@exit-in-use.example:80"
        assert seen[0][1] == "www.holland2stay.com"

    def test_no_proxy_configured_says_so(self, monkeypatch):
        import config

        monkeypatch.setattr(
            config, "probe_proxy",
            lambda *a, **kw: pytest.fail("没配代理却去探代理"),
        )
        msg = self._fetcher("")._describe_navigation_failure(
            Exception("net::ERR_TUNNEL_CONNECTION_FAILED")
        )
        assert "并未走代理" in msg


class TestRequestShape:
    def test_sends_connect_with_target_and_auth(self, fake_proxy):
        p = fake_proxy("HTTP/1.1 200 Connection established")
        probe_proxy(f"http://user:pw@127.0.0.1:{p.port}", "example.com", 443)

        raw = p.requests[0].decode()
        assert raw.startswith("CONNECT example.com:443 HTTP/1.1")
        # 有凭据就得带上，否则代理回的是 407 而不是真实状态
        assert "Proxy-Authorization: Basic dXNlcjpwdw==" in raw

    def test_credentials_never_appear_in_the_reason(self, fake_proxy):
        """返回值会被写进日志，不能把密码带出去。"""
        p = fake_proxy("HTTP/1.1 402 Payment Required")
        reason = probe_proxy(
            f"http://secretuser:secretpw@127.0.0.1:{p.port}", "example.com"
        )
        assert reason
        assert "secretpw" not in reason
        assert "secretuser" not in reason
