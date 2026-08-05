from __future__ import annotations

import json
from types import MethodType

import pytest

import browser_fetcher
from browser_fetcher import BrowserFetcher
from scrapers.base import BlockedError, UpstreamMaintenanceError

_CHALLENGE_HTML = '<html><script>window._cf_chl_opt = {};</script></html>'
_REAL_HTML = "<html><body>Residences</body></html>"
_CLEARANCE_403 = json.dumps(
    {"error": "Browser verification required", "code": "clearance_required"}
)


class _FakePage:
    def __init__(self, *responses: dict, title: str = "", content: str = ""):
        self.responses = list(responses)
        self.scripts: list[str] = []
        self._title = title
        self._content = content

    def evaluate(self, script: str):
        self.scripts.append(script)
        if self.responses:
            return self.responses.pop(0)
        return {
            "status": 200,
            "ok": True,
            "text": json.dumps({"data": {"ok": True}}),
            "headers": {},
        }

    def title(self):
        return self._title

    def content(self):
        return self._content

    def goto(self, *args, **kwargs):
        return None


class _SeqContentPage(_FakePage):
    """``content()`` 按序列推进，模拟 CF 挑战逐步解开；最后一项之后保持不变。"""

    def __init__(self, contents: list[str], *responses: dict, title: str = ""):
        super().__init__(*responses, title=title)
        self._contents = list(contents)

    def content(self):
        if len(self._contents) > 1:
            return self._contents.pop(0)
        return self._contents[0]


def _make_fetcher(page: _FakePage) -> BrowserFetcher:
    fetcher = BrowserFetcher()
    fetcher._initialized = True
    fetcher._page = page

    def ensure_initialized(self):
        self._initialized = True

    fetcher.ensure_initialized = MethodType(ensure_initialized, fetcher)
    return fetcher


def test_fetch_gql_uses_browser_like_same_origin_request():
    page = _FakePage({
        "status": 200,
        "ok": True,
        "text": json.dumps({"data": {"products": {"items": []}}}),
        "headers": {"content-type": "application/json"},
    })
    fetcher = _make_fetcher(page)

    data = fetcher.fetch_gql("query Test { products { items { sku } } }")

    assert data["data"]["products"]["items"] == []
    script = page.scripts[0]
    assert "credentials: 'include'" in script
    assert "mode: 'same-origin'" in script
    assert "referrer: window.location.href" in script
    assert '"Store": "default"' in script
    assert '"Content-Currency": "EUR"' in script


def test_fetch_gql_refreshes_status_after_403_retry_success():
    page = _FakePage(
        {
            "status": 403,
            "ok": False,
            "text": "Forbidden",
            "headers": {"cf-ray": "test-ray"},
        },
        {
            "status": 200,
            "ok": True,
            "text": json.dumps({"data": {"recovered": True}}),
            "headers": {"content-type": "application/json"},
        },
    )
    fetcher = _make_fetcher(page)

    data = fetcher.fetch_gql("query Test { ok }")

    assert data == {"data": {"recovered": True}}
    assert len(page.scripts) == 2


def test_maintenance_title_raises_upstream_maintenance():
    fetcher = BrowserFetcher()
    fetcher._page = _FakePage(title="H2S-Maintenance")

    with pytest.raises(UpstreamMaintenanceError, match="维护"):
        fetcher._raise_if_maintenance_page()


def test_wait_for_challenge_clear_returns_once_marker_disappears(monkeypatch):
    monkeypatch.setattr(browser_fetcher.time, "sleep", lambda _: None)
    page = _SeqContentPage([_CHALLENGE_HTML, _CHALLENGE_HTML, _REAL_HTML])
    fetcher = BrowserFetcher()
    fetcher._page = page

    fetcher._wait_for_challenge_clear(timeout=10)  # 不抛即为通过


def test_wait_for_challenge_clear_raises_blocked_when_challenge_persists(monkeypatch):
    monkeypatch.setattr(browser_fetcher.time, "sleep", lambda _: None)
    fetcher = BrowserFetcher()
    fetcher._page = _FakePage(content=_CHALLENGE_HTML)

    with pytest.raises(BlockedError, match="挑战"):
        fetcher._wait_for_challenge_clear(timeout=0.01)


def test_ensure_initialized_stays_uninitialized_when_challenge_fails(monkeypatch):
    """挑战没过就不能标记为已初始化。

    回归：旧实现把 filter UI 选择器当判据，等不到只告警并继续设
    ``_initialized = True``，于是挑战没通过也照发 GraphQL，必然 403。
    """
    def _boom(self, timeout=None):
        raise BlockedError("挑战未解开")

    monkeypatch.setattr(BrowserFetcher, "_wait_for_challenge_clear", _boom)
    fetcher = BrowserFetcher()
    fetcher._page = _FakePage()

    with pytest.raises(BlockedError):
        fetcher.ensure_initialized()

    assert fetcher.is_initialized is False


def test_ensure_initialized_renavigates_when_clearance_does_not_land(monkeypatch):
    """一次导航拿不到 clearance 就换一次导航，而不是继续轮询。

    token 由导航签发；轮询只会朝一个拿不到 clearance 的会话打一堆必然 403
    的请求。生产上冷启动因此整轮失败并触发 30 分钟熔断。
    """
    attempts: list[int] = []

    def _flaky(self, attempt):
        attempts.append(attempt)
        if attempt < 3:
            raise BlockedError("clearance 未生效")
        return 1.0, 0.5

    monkeypatch.setattr(BrowserFetcher, "_navigate_and_verify", _flaky)
    fetcher = BrowserFetcher()
    fetcher._page = _FakePage()

    fetcher.ensure_initialized()

    assert attempts == [1, 2, 3]
    assert fetcher.is_initialized is True


def test_ensure_initialized_gives_up_after_max_attempts(monkeypatch):
    attempts: list[int] = []

    def _always_blocked(self, attempt):
        attempts.append(attempt)
        raise BlockedError("clearance 未生效")

    monkeypatch.setattr(BrowserFetcher, "_navigate_and_verify", _always_blocked)
    fetcher = BrowserFetcher()
    fetcher._page = _FakePage()

    with pytest.raises(BlockedError):
        fetcher.ensure_initialized()

    assert attempts == [1, 2, 3]
    assert fetcher.is_initialized is False


def test_ensure_initialized_does_not_retry_platform_maintenance(monkeypatch):
    """平台维护重试多少次都一样，应立刻上抛走维护冷却。"""
    attempts: list[int] = []

    def _maintenance(self, attempt):
        attempts.append(attempt)
        raise UpstreamMaintenanceError("H2S 平台维护中")

    monkeypatch.setattr(BrowserFetcher, "_navigate_and_verify", _maintenance)
    fetcher = BrowserFetcher()
    fetcher._page = _FakePage()

    with pytest.raises(UpstreamMaintenanceError):
        fetcher.ensure_initialized()

    assert attempts == [1]


def test_maintenance_check_skipped_while_still_on_challenge_page():
    """挑战页的标题由 Cloudflare 决定，不能拿它判断 H2S 是否在维护。"""
    fetcher = BrowserFetcher()
    fetcher._page = _FakePage(title="H2S-Maintenance", content=_CHALLENGE_HTML)

    fetcher._raise_if_maintenance_page()  # 不该抛


def test_is_clearance_required_only_matches_transient_403():
    f = BrowserFetcher()  # 默认 H2S profile
    assert f._is_clearance_required({"status": 403, "text": _CLEARANCE_403})
    # 真正的屏蔽：403 但没有任何「还在校验」的标记
    assert not f._is_clearance_required({"status": 403, "text": "Forbidden"})
    # 状态码正常时不该误判
    assert not f._is_clearance_required({"status": 200, "text": "clearance_required"})


def test_clearance_pending_falls_back_to_cf_challenge_markers():
    """没有站点专属标记的 profile，靠 CF 挑战页特征判定。

    Xior 被挡时直接回 CF 挑战页，不像 H2S 那样回自己的 clearance_required
    JSON——两种形态都得认出来，否则会把「重新导航就能好」误判成「IP 被封」。
    """
    from browser_fetcher import XIOR_PROFILE

    f = BrowserFetcher(profile=XIOR_PROFILE)
    assert f._is_clearance_required({
        "status": 403,
        "text": "<html><head><title>Just a moment...</title></head></html>",
    })
    assert f._is_clearance_required({"status": 403, "text": _CHALLENGE_HTML})
    # 403 但不是挑战页 → 真的被封
    assert not f._is_clearance_required({"status": 403, "text": "Access denied"})


def test_fetch_gql_renavigates_when_clearance_expires(monkeypatch):
    """clearance 过期要重新走挑战流程，而不是对着 API 轮询。

    token 由页面通过 CF 挑战时下发，轮询 GraphQL 换不出新 token——
    只会白等到超时，再把一个本可恢复的会话误判成被屏蔽。
    """
    monkeypatch.setattr(browser_fetcher.time, "sleep", lambda _: None)
    page = _FakePage(
        # 长会话中 token 过期
        {"status": 403, "ok": False, "text": _CLEARANCE_403, "headers": {}},
        # 重新初始化后重试成功
        {
            "status": 200,
            "ok": True,
            "text": json.dumps({"data": {"recovered": True}}),
            "headers": {},
        },
    )

    calls: list[int] = []
    fetcher = BrowserFetcher()
    fetcher._initialized = True
    fetcher._page = page

    def ensure_initialized(self):
        calls.append(1)
        self._initialized = True

    fetcher.ensure_initialized = MethodType(ensure_initialized, fetcher)

    data = fetcher.fetch_gql("query Test { ok }")

    assert data == {"data": {"recovered": True}}
    # 开头一次 + clearance 过期后重新导航一次
    assert len(calls) == 2
    # 没有滑到通用 403 分支（那条路径会再多一次 ensure_initialized）
    assert len(page.scripts) == 2


def _patch_launch(monkeypatch, platform_name="Darwin"):
    """替换两个 launch 入口，返回记录 kwargs 的列表。

    **两个都要替**。生产走的是 ``launch_persistent_context``（磁盘缓存能省掉
    九成流量），只替 ``launch`` 会让测试真的拉起一个 Chromium、在 data/ 下写出
    十几兆的 profile——最初就是这么发生的。

    这里让持久化那条路直接抛错，使代码退回临时 profile，从而把断言集中在
    ``launch`` 的 kwargs 上；持久化路径本身由 test_browser_profile_cache.py 覆盖。
    """
    calls: list[dict] = []

    class _FakeBrowser:
        def new_page(self):
            return _FakePage()

    def fake_launch(**kwargs):
        calls.append(kwargs)
        return _FakeBrowser()

    def refuse_persistent(*args, **kwargs):
        raise RuntimeError("测试中不启动持久化浏览器")

    import cloakbrowser

    monkeypatch.setattr(browser_fetcher.platform, "system", lambda: platform_name)
    monkeypatch.setattr(cloakbrowser, "launch", fake_launch)
    monkeypatch.setattr(cloakbrowser, "launch_persistent_context", refuse_persistent)
    return calls


def test_macos_headless_launch_uses_headed_directly(monkeypatch):
    calls = _patch_launch(monkeypatch)

    fetcher = BrowserFetcher(headless=True)
    fetcher.__enter__()

    assert len(calls) == 1
    assert calls[0]["headless"] is False
    assert calls[0]["args"] == []


def test_launch_receives_proxy_explicitly(monkeypatch):
    """代理必须显式传给 launch()，不能只靠环境变量。

    回归：Chromium 对 HTTP_PROXY / HTTPS_PROXY 的解析和 curl_cffi 不一致，
    实测两者会落到**不同出口 IP**（浏览器 79.116.229.115 vs curl_cffi
    213.73.166.139，且都不在 sticky 端点指定的国家）。Cloudflare 的 clearance
    绑 IP，出口不一致就无法共享会话，浏览器还会绕过 get_proxy_url() 的
    故障切换逻辑。
    """
    calls = _patch_launch(monkeypatch)
    monkeypatch.setattr(
        browser_fetcher_config(), "get_proxy_url",
        lambda source="", **kw: "http://u:p@p.webshare.io:80",
    )

    BrowserFetcher(headless=True).__enter__()

    assert calls[0]["proxy"] == "http://u:p@p.webshare.io:80"


def test_launch_proxy_is_none_when_unconfigured(monkeypatch):
    """没配代理时传 None，而不是空串——空串会被当成非法代理。"""
    calls = _patch_launch(monkeypatch)
    monkeypatch.setattr(
        browser_fetcher_config(), "get_proxy_url", lambda source="", **kw: ""
    )

    BrowserFetcher(headless=True).__enter__()

    assert calls[0]["proxy"] is None


def browser_fetcher_config():
    """browser_fetcher 延迟 import config，测试要 patch 的是 config 模块本身。"""
    import config

    return config


@pytest.mark.parametrize("url,expected", [
    ("http://user:secret@p.webshare.io:80", "p.webshare.io:80"),
    ("http://proxy.local:8080", "http://proxy.local:8080"),
    ("", ""),
])
def test_proxy_redaction_strips_credentials(url, expected):
    """日志里不能出现代理的用户名密码。"""
    assert browser_fetcher._redact_proxy(url) == expected


def test_rotating_profile_requests_a_fresh_proxy_session(monkeypatch):
    """两个 profile 建浏览器时都要换出口 IP，理由不同但结论一致。

    Xior：AJAX 端点按 IP 累积限流，固定出口跑几轮必然 429（实测第 2 轮第一个
    请求即被拒）。换浏览器就换 IP，把累积量摊开。

    H2S：2026-08-03 生产事故——出口 IP 被 CF 盯上后连续 3 次 90s 挑战全失败，
    熔断 30 分钟。而 sticky session id 是 sha1(source) 的**常量**，403 之后
    invalidate_session() 重建拿到的还是同一个 IP，恢复路径形同虚设。

    换 IP 不损失 clearance 复用：clearance 只在单个浏览器生命周期内有效
    （_BROWSER_MAX_AGE=2h），这期间 IP 依然稳定；而**重建浏览器本来就要重解
    挑战**，那一刻换个新 IP 是免费的。
    """
    from browser_fetcher import XIOR_PROFILE, H2S_PROFILE

    calls = _patch_launch(monkeypatch)
    seen: list[bool] = []
    monkeypatch.setattr(
        browser_fetcher_config(), "get_proxy_url",
        lambda source="", *, rotating=False: (
            seen.append(rotating) or "http://u:p@h:80"
        ),
    )

    BrowserFetcher(headless=True, profile=XIOR_PROFILE).__enter__()
    BrowserFetcher(headless=True, profile=H2S_PROFILE).__enter__()

    assert seen == [True, True], seen
    assert len(calls) == 2


def test_sticky_session_id_is_constant_so_rotation_is_the_only_escape():
    """回归说明：sticky 模式下 session id 恒定，被烧的 IP 换不掉。

    这正是 2026-08-03 H2S 事故的根因，也是两个 profile 都开 rotating 的理由。
    """
    from config import _derive_session_id

    sticky = {_derive_session_id("holland2stay", False) for _ in range(5)}
    assert len(sticky) == 1, "sticky 必须恒定——否则 clearance 在会话内就不稳了"

    rot = {_derive_session_id("holland2stay", True) for _ in range(5)}
    assert len(rot) == 5, "rotating 每次都要给出新 session，才能真正换掉被烧的 IP"
