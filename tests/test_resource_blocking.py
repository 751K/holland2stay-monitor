"""浏览器只需要 DOM 和 cf_clearance，不需要图片、字体和统计脚本。

2026-08-04 全天代理侧记录：985MB 流量里约 97MB 是页面自己拉的第三方资源与
静态文件，与房源数据无关。代理按流量计费，这部分是纯支出。

拦截的风险全在「拦过头」：拦掉挑战需要的东西，抓取直接停摆；拦掉站点自身的
业务 JS，页面渲染不出来。所以这里的用例分两类——该拦的要拦住，不该拦的一个
都不能碰。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from browser_fetcher import (
    H2S_PROFILE,
    BrowserFetcher,
    _should_block,
)


class TestBlocksWaste:
    @pytest.mark.parametrize("url", [
        "https://media.holland2stay.com/catalog/product/x.jpg",
        "https://www.holland2stay.com/static/logo.png",
    ])
    def test_images_blocked(self, url):
        assert _should_block(url, "image")

    def test_fonts_blocked_by_type(self):
        assert _should_block("https://www.xiorstudenthousing.eu/f/x.woff2", "font")

    def test_video_blocked(self):
        assert _should_block("https://www.holland2stay.com/tour.mp4", "media")

    @pytest.mark.parametrize("host", [
        "www.googletagmanager.com",
        "region1.google-analytics.com",
        "pagead2.googlesyndication.com",
        "fonts.gstatic.com",
        "fonts.googleapis.com",
        "ka-p.fontawesome.com",
        "kit.fontawesome.com",
        "cdn-cookieyes.com",
        "log.cookieyes.com",
        "widget.trustpilot.com",
        "www.chatbase.co",
        "analytics.ahrefs.com",
        "photon.komoot.io",
    ])
    def test_third_party_blocked_regardless_of_type(self, host):
        """这些域整体拦掉，脚本也不放行——它们不参与页面渲染。"""
        assert _should_block(f"https://{host}/whatever.js", "script")

    def test_covers_the_measured_waste(self):
        """按 2026-08-04 的实测量核对，确认拦截清单没有漏掉大头。

        数字是当天代理侧记录的每日流量（MB）。清单改动后若这里跌下来，
        说明省下的钱变少了，应当是有意为之而不是顺手删的。
        """
        measured = {
            "media.holland2stay.com": (32.0, "image"),
            "www.googletagmanager.com": (24.3, "script"),
            "fonts.gstatic.com": (15.2, "font"),
            "ka-p.fontawesome.com": (8.7, "script"),
            "cdn-cookieyes.com": (3.1, "script"),
            "widget.trustpilot.com": (1.8, "script"),
            "region1.google-analytics.com": (1.3, "script"),
            "www.chatbase.co": (1.2, "script"),
            "analytics.ahrefs.com": (1.1, "script"),
            "photon.komoot.io": (0.9, "xhr"),
            "fonts.googleapis.com": (0.8, "stylesheet"),
            "pagead2.googlesyndication.com": (0.7, "script"),
            "kit.fontawesome.com": (0.6, "script"),
            "log.cookieyes.com": (0.5, "script"),
        }
        saved = sum(
            mb for host, (mb, rtype) in measured.items()
            if _should_block(f"https://{host}/a", rtype)
        )
        assert saved > 90, f"只拦下 {saved:.1f}MB/天，清单可能被削过头"


class TestNeverBlocksWhatTheChallengeNeeds:
    @pytest.mark.parametrize("host", [
        "challenges.cloudflare.com",
        "brunhild.challenges.cloudflare.com",
        "hagen.challenges.cloudflare.com",
        "static.cloudflareinsights.com",
    ])
    @pytest.mark.parametrize("rtype", ["script", "image", "font", "xhr"])
    def test_cloudflare_always_allowed(self, host, rtype):
        """挑战域一律放行，包括本会按类型拦掉的图片和字体。"""
        assert not _should_block(f"https://{host}/turnstile/v0/api.js", rtype)

    @pytest.mark.parametrize("url", [
        "https://www.holland2stay.com/residences",
        "https://api.holland2stay.com/api/graphql",
        "https://www.xiorstudenthousing.eu/wp-admin/admin-ajax.php",
        "https://thisisourdomain.securerc.co.uk/onlineleasing/x/floorplans.aspx",
    ])
    def test_business_endpoints_allowed(self, url):
        assert not _should_block(url, "document")
        assert not _should_block(url, "xhr")
        assert not _should_block(url, "fetch")

    def test_site_scripts_and_styles_allowed(self):
        """站点自身的 JS/CSS 不能拦——页面渲染不出来，CF 的行为检测也会失真。"""
        assert not _should_block("https://www.holland2stay.com/app.js", "script")
        assert not _should_block("https://www.holland2stay.com/app.css", "stylesheet")

    def test_shared_cdn_allowed(self):
        """jsdelivr 同时供着站点自己的 bundle，从域名分不出来，只能放行。"""
        assert not _should_block("https://cdn.jsdelivr.net/npm/x.js", "script")

    def test_lookalike_domain_is_not_matched(self):
        """后缀匹配必须按点边界，否则 evil-trustpilot.com 之类会被误判。"""
        assert not _should_block("https://nottrustpilot.com/a.js", "script")
        assert _should_block("https://widget.trustpilot.com/a.js", "script")


class TestMalformedInput:
    @pytest.mark.parametrize("url", ["", "not a url", "about:blank", "data:text/html,x"])
    def test_unparseable_url_is_allowed(self, url):
        """判不出来就放行：拦错一个必要请求的代价远高于多下一次。"""
        assert not _should_block(url, "image")


class _FakeRequest:
    def __init__(self, url, resource_type):
        self.url = url
        self.resource_type = resource_type


class _FakeRoute:
    def __init__(self, url, resource_type):
        self.request = _FakeRequest(url, resource_type)
        self.aborted = False
        self.continued = False

    def abort(self):
        self.aborted = True

    def continue_(self):
        self.continued = True


class _FakePage:
    def __init__(self):
        self.handler = None

    def route(self, pattern, handler):
        self.pattern = pattern
        self.handler = handler


def _fetcher_with_route(monkeypatch, env: str | None = None):
    if env is None:
        monkeypatch.delenv("BROWSER_BLOCK_RESOURCES", raising=False)
    else:
        monkeypatch.setenv("BROWSER_BLOCK_RESOURCES", env)
    f = BrowserFetcher(profile=H2S_PROFILE)
    f._page = _FakePage()
    f._install_resource_blocking()
    return f


class TestHandlerWiring:
    def test_blocked_request_is_aborted_and_counted(self, monkeypatch):
        f = _fetcher_with_route(monkeypatch)
        route = _FakeRoute("https://media.holland2stay.com/a.jpg", "image")
        f._page.handler(route)

        assert route.aborted and not route.continued
        assert f._blocked_count == 1

    def test_allowed_request_continues(self, monkeypatch):
        f = _fetcher_with_route(monkeypatch)
        route = _FakeRoute("https://api.holland2stay.com/api/graphql", "fetch")
        f._page.handler(route)

        assert route.continued and not route.aborted
        assert f._blocked_count == 0

    def test_handler_never_leaves_a_request_hanging(self, monkeypatch):
        """判定逻辑抛异常时必须放行，不能让请求悬到超时。"""
        import browser_fetcher

        f = _fetcher_with_route(monkeypatch)
        monkeypatch.setattr(
            browser_fetcher, "_should_block",
            lambda *a: (_ for _ in ()).throw(RuntimeError("判定炸了")),
        )
        route = _FakeRoute("https://media.holland2stay.com/a.jpg", "image")
        f._page.handler(route)

        assert route.continued, "判定异常导致请求既没拦也没放"

    def test_can_be_switched_off_without_a_release(self, monkeypatch):
        """拦截改变了加载行为，CF 起疑时要能不重新发版就退回原状。"""
        f = _fetcher_with_route(monkeypatch, env="0")
        assert f._page.handler is None

    def test_on_by_default(self, monkeypatch):
        assert _fetcher_with_route(monkeypatch)._page.handler is not None

    def test_route_failure_does_not_break_launch(self, monkeypatch):
        """拦截是省钱的优化，装不上就照常抓，不能把浏览器起不来。"""
        monkeypatch.delenv("BROWSER_BLOCK_RESOURCES", raising=False)

        class _BadPage:
            def route(self, *a):
                raise RuntimeError("route 不可用")

        f = BrowserFetcher(profile=H2S_PROFILE)
        f._page = _BadPage()
        f._install_resource_blocking()  # 不抛即通过
