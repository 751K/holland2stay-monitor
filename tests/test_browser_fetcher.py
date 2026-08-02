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


def test_macos_headless_launch_uses_headed_directly(monkeypatch):
    calls: list[dict] = []

    class _FakeBrowser:
        def new_page(self):
            return _FakePage()

    def fake_launch(**kwargs):
        calls.append(kwargs)
        return _FakeBrowser()

    import cloakbrowser

    monkeypatch.setattr(browser_fetcher.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(cloakbrowser, "launch", fake_launch)

    fetcher = BrowserFetcher(headless=True)
    fetcher.__enter__()

    assert len(calls) == 1
    assert calls[0]["headless"] is False
    assert calls[0]["args"] == []
