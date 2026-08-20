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


@pytest.fixture(autouse=True)
def _clear_enc_pubkey_cache():
    """公钥缓存是进程级的（见 browser_fetcher._ENC_PUBKEY_CACHE）。

    不清就会跨测试泄漏：第一个用例抓完之后，后面的 key_fetches 全是 0，
    「每会话只抓一次」这类断言会变成永远通过。
    """
    from browser_fetcher import _ENC_PUBKEY_CACHE
    _ENC_PUBKEY_CACHE.clear()
    yield
    _ENC_PUBKEY_CACHE.clear()


class _FakePage:
    def __init__(self, *responses: dict, title: str = "", content: str = ""):
        self.responses = list(responses)
        self.scripts: list[str] = []
        self.args: list = []          # 每次请求 evaluate 的参数，供断言用
        self.key_fetches = 0
        self.key_hints: list = []
        self._title = title
        self._content = content

    #: 公钥抓取（``_ensure_enc_pubkey``）返回的假 SPKI 常量。
    #: 它走的也是 page.evaluate，但返回的是字符串而不是响应 dict——
    #: 按参数个数区分：抓公钥那次带一个正则参数。
    ENC_PUBKEY = "MII" + "A" * 100

    def evaluate(self, script: str, arg=None):
        # _ensure_enc_pubkey：唯一一个传 [正则, chunk 名单] 且不发请求的
        # evaluate。单独计数——``scripts`` 是「发了几次请求」的判据，把抓公钥
        # 混进去会让那些计数断言变成在数实现细节。
        if (isinstance(arg, list) and len(arg) == 2
                and isinstance(arg[0], str) and "MII" in arg[0]):
            self.key_fetches += 1
            self.key_hints = arg[1]
            return {"key": self.ENC_PUBKEY, "scanned": 1, "fallback": False}
        self.scripts.append(script)
        self.args.append(arg)
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
    # H2S 自 2026-08-17 起走加密信封（_encrypted_fetch），请求头改为**参数**
    # 传入而不是拼进 JS 源码。所以断言分两处：形状看源码，内容看实参——后者
    # 比匹配源码子串结实，改写法不会误报。
    script = page.scripts[0]
    assert 'credentials: "include"' in script
    assert 'mode: "same-origin"' in script
    assert "referrer: window.location.href" in script

    a = page.args[0]
    assert a["pub"] == page.ENC_PUBKEY, "没有把抓到的公钥传下去"
    assert a["url"] == "/api/__enc__"
    assert a["method"] == "POST"
    assert a["headers"]["Store"] == "default"
    assert a["headers"]["Content-Currency"] == "EUR"
    assert a["headers"][a["encHeader"]] == "1", "信封请求必须带 x-enc: 1"
    assert "query Test" in a["plaintext"], "加密的应当是原始 GraphQL 明文"
    assert not a["queryHeader"], "GraphQL 的信封走 body，不走 x-enc-q"


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


# ── 加密信封传输 ────────────────────────────────────────────────────
#
# 2026-08-17 H2S 把 GraphQL 整体搬进加密信道：明文请求直接吃 Cloudflare 挑战。
# 这是同一 API 的第三次迁移，前两次都只是换路径。
#
# 传输层之上不该有任何感知——_GQL_QUERY 与 _to_listing 一行没改，因为截获站点
# 加密前的明文可见它发的仍是同一条 GetCategories 查询。以下用例锁住这个边界。

class TestFetchPlain:
    """NextAuth 端点必须走明文，即便 H2S profile 默认开着加密信封。

    套上信封发到 /api/auth/* 只会 400——登录整条就断在第一步。
    """

    def test_post_bypasses_the_envelope(self):
        # H2S profile：encrypted_envelope=True。fetch_plain(POST) 仍应走明文
        # _raw_fetch（page.evaluate 单参数、arg 为 None），而不是加密路径
        # （6 元素 list 参数）。
        page = _FakePage({
            "status": 200, "ok": True,
            "text": json.dumps({"url": "https://www.holland2stay.com/"}),
            "headers": {},
        })
        fetcher = _make_fetcher(page)
        assert fetcher._profile.encrypted_envelope is True

        fetcher.fetch_plain("/api/auth/callback/credentials", method="POST",
                            body="email=a%40b.com&password=x",
                            headers={"Content-Type": "application/x-www-form-urlencoded"})

        # 加密路径会往 args 里塞一个 6 元素 list [pub, path, body, hdrs, t, encHdr]。
        # 明文路径的 evaluate 是单参数，arg 记为 None。
        assert not any(isinstance(a, list) and len(a) == 6 for a in page.args), (
            "fetch_plain 走了加密信封——NextAuth 端点会被 400"
        )
        assert page.key_fetches == 0, "明文请求不该去抓加密公钥"

    def test_get_returns_raw_body(self):
        page = _FakePage({
            "status": 200, "ok": True,
            "text": json.dumps({"csrfToken": "abc"}),
            "headers": {},
        })
        fetcher = _make_fetcher(page)
        r = fetcher.fetch_plain("/api/auth/csrf", method="GET")
        assert r["status"] == 200
        assert json.loads(r["text"])["csrfToken"] == "abc"


class TestEncryptedEnvelope:
    def test_only_h2s_encrypts(self):
        """Xior 不走信封。开关放在 profile 上，别让它变成全局行为。"""
        from browser_fetcher import H2S_PROFILE, XIOR_PROFILE
        assert H2S_PROFILE.encrypted_envelope is True
        assert XIOR_PROFILE.encrypted_envelope is False

    def test_plaintext_query_is_what_gets_encrypted(self):
        """加密的必须是原始 GraphQL，不是别的形状。

        站点自己发的明文（截获自 crypto.subtle.encrypt）就是
        {"query": ..., "variables": ...}，schema 完全没变。
        """
        page = _FakePage({
            "status": 200, "ok": True,
            "text": json.dumps({"data": {"products": {"items": []}}}),
            "headers": {"x-enc": "1"},
        })
        fetcher = _make_fetcher(page)
        fetcher.fetch_gql("query Q { products { items { sku } } }",
                          {"pageSize": 100})

        sent = json.loads(page.args[0]["plaintext"])
        assert sent["query"] == "query Q { products { items { sku } } }"
        assert sent["variables"] == {"pageSize": 100}

    def test_pubkey_fetched_once_per_session(self):
        """公钥每个浏览器会话抓一次就够，不该每请求都扫一遍 bundle。"""
        page = _FakePage(
            {"status": 200, "ok": True, "text": json.dumps({"data": {}}),
             "headers": {"x-enc": "1"}},
            {"status": 200, "ok": True, "text": json.dumps({"data": {}}),
             "headers": {"x-enc": "1"}},
        )
        fetcher = _make_fetcher(page)
        fetcher.fetch_gql("query A { a }")
        fetcher.fetch_gql("query B { b }")
        assert page.key_fetches == 1, f"抓了 {page.key_fetches} 次公钥"
        assert len(page.scripts) == 2

    def test_missing_pubkey_says_where_to_look(self):
        """抓不到公钥要指出去哪儿找——bundle 结构变了时这是唯一的线索。"""
        from scrapers.base import ScrapeNetworkError

        class _NoKeyPage(_FakePage):
            def evaluate(self, script, arg=None):
                if (isinstance(arg, list) and len(arg) == 2
                        and isinstance(arg[0], str) and "MII" in arg[0]):
                    return None
                return super().evaluate(script, arg)

        fetcher = _make_fetcher(_NoKeyPage())
        with pytest.raises(ScrapeNetworkError) as ei:
            fetcher.fetch_gql("query Q { q }")
        msg = str(ei.value)
        assert "公钥" in msg
        assert "__enc__" in msg, "没说去哪个 chunk 找"

    def test_only_scans_named_chunks(self):
        """按 chunk 名字筛，别遍历全部 script。

        2026-08-18 初版就是遍历全部（实测 81 个），每建一次浏览器多打 ~97 个
        请求，当晚 H2S 就被 Cloudflare 连续 403。这条守住那次教训。
        """
        from browser_fetcher import _ENC_CHUNK_HINTS

        page = _FakePage({"status": 200, "ok": True,
                          "text": json.dumps({"data": {}}),
                          "headers": {"x-enc": "1"}})
        _make_fetcher(page).fetch_gql("query A { a }")

        assert page.key_hints == list(_ENC_CHUNK_HINTS)
        assert page.key_hints, "没有传 chunk 名单 = 又在扫全部 script"

    def test_pubkey_survives_browser_rebuild(self):
        """公钥是站点常量，进程内共享，**不该**随浏览器重建作废。

        初版绑在浏览器生命周期上，而 403 恰恰会触发重建——被封之后反而扫得
        更凶，正反馈。
        """
        import inspect
        from browser_fetcher import BrowserFetcher
        src = inspect.getsource(BrowserFetcher._launch)
        assert "_enc_pubkey" not in src, "_launch 又在丢公钥缓存了"

    def test_crypto_failure_drops_the_key(self):
        """轮换的自愈路径：加解密失败 → 作废缓存 → 下次重抓。"""
        from browser_fetcher import _ENC_PUBKEY_CACHE

        page = _FakePage(
            {"error": "The operation failed for an operation-specific reason"},
            {"status": 200, "ok": True, "text": json.dumps({"data": {}}),
             "headers": {"x-enc": "1"}},
        )
        fetcher = _make_fetcher(page)
        try:
            fetcher.fetch_gql("query A { a }")
        except Exception:
            pass
        assert not _ENC_PUBKEY_CACHE, "解密失败后没有作废公钥，轮换将无法自愈"

    def test_clearance_probe_path_is_the_envelope_endpoint(self):
        """探测请求也得走信封，否则初始化永远探不通。"""
        from browser_fetcher import H2S_PROFILE, _H2S_GQL_PATH
        assert _H2S_GQL_PATH == "/api/__enc__"
        assert H2S_PROFILE.clearance_probe.path == _H2S_GQL_PATH

    def test_get_requests_skip_the_envelope(self):
        """GET 没有 body，包不了信封，也不该被这条分支拦下。"""
        page = _FakePage({"status": 200, "ok": True, "text": "{}", "headers": {}})
        fetcher = _make_fetcher(page)
        fetcher._raw_fetch("/whatever", method="GET")
        assert page.key_fetches == 0, "GET 不该去抓公钥"
        assert "async () =>" in page.scripts[0], "GET 应当走明文 _raw_fetch"


class TestRestEnvelope:
    """``/api/rest/*`` 的信封约定与 GraphQL / NextAuth 都不同，三者别混。

    2026-08-19 逐字读自站点 module 82361（函数 H / J）：
        GET  /api/rest/X  → 加密**路径**（去掉 /api 前缀），base64 塞 x-enc-q，
                            实际请求 GET /api/rest/__enc__
        POST /api/rest/X  → 加密 **body**，POST 原 URL，x-enc: 1
        /api/auth/*       → 完全不加密（拦截器只对 /api/rest/ 生效）

    cancel_pending_orders 曾经对 /api/rest/* 直接发明文，两条都不对。
    """

    def test_get_encrypts_the_path_into_x_enc_q(self):
        page = _FakePage({
            "status": 200, "ok": True,
            "text": json.dumps({"items": []}),
            "headers": {"x-enc": "1"},
        })
        fetcher = _make_fetcher(page)
        fetcher.fetch_rest("/api/rest/V1/newdashboard/contract/me?fields=x",
                           method="GET")

        a = page.args[0]
        assert a["plaintext"] == "/rest/V1/newdashboard/contract/me?fields=x", (
            "加密的必须是去掉 /api 前缀的路径（含 query），站点原文如此"
        )
        assert a["contentType"] == "text/plain", (
            "GET 那条的 ct 是 text/plain，写成 application/json 服务端解不开"
        )
        assert a["url"] == "/api/rest/__enc__", "GET 必须打到 __enc__ 端点"
        assert a["method"] == "GET"
        assert a["queryHeader"] == "x-enc-q", "信封必须走头，不是 body"

    def test_post_encrypts_the_body_at_the_original_url(self):
        page = _FakePage({
            "status": 200, "ok": True, "text": json.dumps({"ok": True}),
            "headers": {"x-enc": "1"},
        })
        fetcher = _make_fetcher(page)
        fetcher.fetch_rest("/api/rest/V1/customer/bookingcancel/r-x-1",
                           method="POST", body="{}")

        a = page.args[0]
        assert a["url"] == "/api/rest/V1/customer/bookingcancel/r-x-1", (
            "POST 必须打原 URL，不是 __enc__"
        )
        assert a["method"] == "POST"
        assert a["plaintext"] == "{}", "加密的是 body"
        assert a["contentType"] == "application/json"
        assert not a["queryHeader"], "POST 那条走 body，不走 x-enc-q"

    def test_auth_endpoints_stay_plain(self):
        """反向守卫：/api/auth/* 不该被加密。写反了登录第一步就 400。"""
        page = _FakePage({
            "status": 200, "ok": True,
            "text": json.dumps({"csrfToken": "abc"}), "headers": {},
        })
        fetcher = _make_fetcher(page)
        fetcher.fetch_plain("/api/auth/csrf", method="GET")
        assert not any(isinstance(a, dict) and "plaintext" in a for a in page.args), (
            "NextAuth 端点被套上了信封"
        )


class TestEnvelopeHasOneImplementation:
    """信封的密码学只准有一份实现。

    之前是两份各 90+ 行、行级 76% 重复的拷贝（``_encrypted_fetch`` 与
    ``_encrypted_rest_get``）。站点对两种投递方式用的是同一套密码学，只在三处
    不同——加密什么、``ct`` 写什么、信封放 body 还是放头。剩下的全是共享的：
    RSA-OAEP 包裹 AES 会话密钥、12 字节 IV、同源 fetch 的凭据设置、响应头白
    名单、``x-enc`` 判定与解密。

    两份拷贝意味着公钥轮换自愈、错误收敛、响应头白名单各写了两遍——改一处忘
    另一处只是时间问题。
    """

    def test_only_one_place_calls_crypto_subtle(self):
        import re
        from pathlib import Path

        src = Path("browser_fetcher.py").read_text(encoding="utf-8")
        # 建 AES 会话密钥是信封的标志动作，出现两次就是又抄了一份
        n = len(re.findall(r'generateKey\(\{name:\s*"AES-GCM"', src))
        assert n == 1, f"信封的 AES 密钥生成出现了 {n} 次——又抄了一份实现"

    def test_both_delivery_modes_go_through_the_shared_helper(self):
        from browser_fetcher import BrowserFetcher

        calls = []

        class _Spy(BrowserFetcher):
            def _envelope_fetch(self, **kw):
                calls.append(kw)
                return {"status": 200, "ok": True, "text": "{}", "headers": {}}

        f = _Spy.__new__(_Spy)
        f._encrypted_fetch("/api/__enc__", body="{}", headers={}, timeout_ms=1)
        f._encrypted_rest_get("/api/rest/V1/x", headers={}, timeout_ms=1)
        assert len(calls) == 2, "有一条没走共享实现"
        body_mode, head_mode = calls
        # body 投递：不传 query_header（走默认空串）
        assert not body_mode.get("query_header")
        assert body_mode["content_type"] == "application/json"
        assert body_mode["method"] == "POST"
        # 头投递：x-enc-q + text/plain，这两处写混了服务端解不开
        assert head_mode["query_header"] == "x-enc-q"
        assert head_mode["content_type"] == "text/plain"
        assert head_mode["method"] == "GET"

    def test_pubkey_self_heal_is_not_duplicated(self):
        """公钥作废（轮换自愈）只该有一个触发点。"""
        from pathlib import Path

        src = Path("browser_fetcher.py").read_text(encoding="utf-8")
        n = src.count("self._drop_enc_pubkey()")
        assert n == 1, (
            f"_drop_enc_pubkey 有 {n} 个调用点——两份拷贝各写一遍正是要消掉的问题"
        )
