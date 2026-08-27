"""``Link:`` 响应头——Cloudflare 生成 103 Early Hints 的原料。

2026-08-27 实测：zone 上 ``early_hints`` 开着，而源站一个 ``Link:`` 都没有，
边缘因此什么都播不出来。这组用例守的是「播出去的东西和这一页真正加载的东西
一致」——多播会白占带宽并让浏览器报 preloaded but not used，少播则等于没做。
"""
from __future__ import annotations

import re

import pytest

from app.early_hints import _MAX_PRELOADS, _stylesheets, add_link_headers

PUBLIC_PATHS = ("/login", "/guide", "/donate", "/support", "/privacy", "/terms")

_PRELOAD_RE = re.compile(r"^<([^>]+)>; rel=preload; as=style$")


def _links(resp) -> list[str]:
    return resp.headers.getlist("Link")


def _preloaded(resp) -> list[str]:
    out = []
    for h in _links(resp):
        m = _PRELOAD_RE.match(h)
        if m:
            out.append(m.group(1))
    return out


class TestExtraction:
    """``_stylesheets`` 是这套东西的判据本身，单独测。"""

    def test_finds_rel_before_href(self):
        body = b'<link rel="stylesheet" href="/a.css">'
        assert _stylesheets(body) == ["/a.css"]

    def test_finds_href_before_rel(self):
        """donate.html 就是这个顺序；用一条带顺序的正则会漏掉它。"""
        body = b'<link href="/b.css" rel="stylesheet">'
        assert _stylesheets(body) == ["/b.css"]

    def test_spans_newlines(self):
        """base.html 把 href 换行写在下一行。"""
        body = b'<link rel="stylesheet"\n      href="/c.css">'
        assert _stylesheets(body) == ["/c.css"]

    def test_ignores_non_stylesheet_links(self):
        """icon / apple-touch-icon / preload 都是 <link>，但不是样式表。"""
        body = (b'<link rel="icon" href="/favicon.png">'
                b'<link rel="apple-touch-icon" href="/touch.png">'
                b'<link rel="preload" href="/d.css" as="style">'
                b'<link rel="stylesheet" href="/d.css">')
        assert _stylesheets(body) == ["/d.css"]

    def test_unescapes_html_entities(self):
        """Jinja 会把 href 里的 & 转义成 &amp;。

        原样放进 Link 头就是一个指向不存在资源的 URL——Google Fonts 的
        ``css2?family=…&display=swap`` 每一个都中招，而且浏览器不会报错，
        只是白下载一份 404，看不出来。
        """
        body = b'<link rel="stylesheet" href="https://x/css2?family=Inter&amp;display=swap">'
        assert _stylesheets(body) == ["https://x/css2?family=Inter&display=swap"]

    def test_dedupes(self):
        body = b'<link rel="stylesheet" href="/a.css"><link rel="stylesheet" href="/a.css">'
        assert _stylesheets(body) == ["/a.css"]

    def test_keeps_document_order(self):
        body = (b'<link rel="stylesheet" href="/1.css">'
                b'<link rel="stylesheet" href="/2.css">')
        assert _stylesheets(body) == ["/1.css", "/2.css"]

    def test_empty_when_no_stylesheets(self):
        assert _stylesheets(b"<html><head></head></html>") == []


class TestRenderedPages:
    @pytest.mark.parametrize("path", PUBLIC_PATHS)
    def test_preconnects_to_both_font_hosts(self, client, path):
        links = _links(client.get(path))
        assert "<https://fonts.googleapis.com>; rel=preconnect" in links
        assert "<https://fonts.gstatic.com>; rel=preconnect; crossorigin" in links

    def test_gstatic_preconnect_is_crossorigin(self, client):
        """字体请求是匿名 CORS 的。不带 crossorigin，浏览器会另开一条连接，
        preconnect 等于白做——而且不会有任何报错。
        """
        links = _links(client.get("/login"))
        gstatic = [h for h in links if "fonts.gstatic.com" in h]
        assert gstatic and all("crossorigin" in h for h in gstatic)

    @pytest.mark.parametrize("path", PUBLIC_PATHS)
    def test_every_preload_is_actually_used_by_the_page(self, client, path):
        """多播的代价是真实的：浏览器会下载它、占掉首屏带宽，再报
        "preloaded but not used"。
        """
        r = client.get(path)
        body = r.get_data(as_text=True)
        for url in _preloaded(r):
            assert url.replace("&", "&amp;") in body or url in body, (
                f"{path} 预载了 {url}，但页面里根本没有引用它"
            )

    @pytest.mark.parametrize("path", PUBLIC_PATHS)
    def test_every_stylesheet_on_the_page_is_preloaded(self, client, path):
        """反向：漏掉就等于没做。"""
        r = client.get(path)
        expected = _stylesheets(r.get_data())[:_MAX_PRELOADS]
        assert expected, f"{path} 一个样式表都没有，这条用例测了个空"
        assert _preloaded(r) == expected

    def test_pages_differ_from_each_other(self, client):
        """判据是「这一页真正加载了什么」，不是一份统一清单。

        /login 有 design.css 和图标字体；donate / legal 刻意不引 design.css。
        两边播一样的东西，说明实现退化成了写死的清单。
        """
        login = set(_preloaded(client.get("/login")))
        donate = set(_preloaded(client.get("/donate")))
        assert any("design.css" in u for u in login)
        assert not any("design.css" in u for u in donate)

    def test_font_weights_follow_the_page(self, client):
        """连字重参数都不一样：base/login 是 400;450;500;550;600;700，
        donate 是 400;500;600;700。播错等于预载一份用不上的 CSS。
        """
        def font(resp):
            return [u for u in _preloaded(resp) if "fonts.googleapis.com" in u]
        assert font(client.get("/login")) != font(client.get("/donate"))

    @pytest.mark.parametrize("path", PUBLIC_PATHS)
    def test_no_html_entities_leak_into_headers(self, client, path):
        for h in _links(client.get(path)):
            assert "&amp;" not in h, f"{path} 的 Link 头里有 &amp;，URL 是坏的"

    def test_does_not_preload_the_logo(self, client):
        """深浅色是两个文件，主题由内联脚本在运行时才定。

        Link 头比 HTML 更早，没有条件判断的余地——写死任何一张都必然有一半
        用户预载错的那张。base.html 不把它写进 HTML、改由脚本动态插入，就是
        这个原因。
        """
        for h in _links(client.get("/login")):
            assert "logo" not in h


class TestScope:
    def test_not_added_to_redirects(self, client):
        """``/`` 对匿名访客 302 到登录页。那个路径的资源该由目标页自己声明。"""
        r = client.get("/")
        assert r.status_code == 302
        assert not _links(r)

    def test_not_added_to_json_apis(self, admin_client):
        """必须用**真的返回 200** 的 JSON 响应。

        这条原先用匿名 client，而 /api/status 对匿名访客返回 401——状态码那道
        关先挡住了，mimetype 那道关根本没被走到。变异测试里把 mimetype 判断
        整条删掉，用例照样绿。
        """
        r = admin_client.get("/api/status")
        assert r.status_code == 200 and r.mimetype == "application/json", (
            "这条要的是 200 的 JSON，否则测的是状态码那道关"
        )
        assert not _links(r)

    def test_not_added_to_static_files(self, client):
        r = client.get("/static/og-card.png")
        assert r.status_code == 200
        assert not _links(r)

    def test_does_not_clobber_an_existing_link_header(self, test_app):
        """视图自己声明过 Link 的话不该被吞掉——响应头的公共约定。"""
        from flask import Response
        with test_app.test_request_context("/"):
            resp = Response("<link rel=\"stylesheet\" href=\"/a.css\">",
                            mimetype="text/html")
            resp.headers.add("Link", "<https://example.test>; rel=dns-prefetch")
            add_link_headers(resp)
        links = resp.headers.getlist("Link")
        assert "<https://example.test>; rel=dns-prefetch" in links
        assert "</a.css>; rel=preload; as=style" in links


class TestStreamingIsUntouched:
    """SSE 绝对不能碰。

    ``get_data()`` 会把生成器抽干：连接当场变成一个已经结束的空响应，
    /api/events 和移动端的 /api/v1/notifications/stream 全部当场失效。
    """

    def test_streamed_response_gets_no_link_header(self, test_app):
        from flask import Response
        with test_app.test_request_context("/"):
            resp = Response((c for c in ["a", "b"]), mimetype="text/html")
            assert resp.is_streamed, "构造的不是流式响应，这条用例测了个空"
            add_link_headers(resp)
        assert not resp.headers.getlist("Link")

    def test_streamed_body_is_not_consumed(self, test_app):
        """判据不能只看有没有 Link 头——先 get_data() 再判断也一样没有头，
        但流已经被抽干了。这里断言正文还在。
        """
        from flask import Response
        with test_app.test_request_context("/"):
            resp = Response((c for c in ["hello", " ", "world"]), mimetype="text/html")
            add_link_headers(resp)
            assert "".join(resp.response) == "hello world"

    def test_passthrough_response_is_skipped(self, test_app):
        """send_file 走 direct_passthrough，读它会破坏文件句柄。"""
        from flask import Response
        with test_app.test_request_context("/"):
            resp = Response("<link rel=\"stylesheet\" href=\"/a.css\">",
                            mimetype="text/html")
            resp.direct_passthrough = True
            add_link_headers(resp)
        assert not resp.headers.getlist("Link")


class TestCap:
    def test_caps_the_number_of_preloads(self, test_app):
        """Early Hints 抢的是带宽：列太多会和 HTML 本身争抢，首屏反而更慢。"""
        from flask import Response
        body = "".join(f'<link rel="stylesheet" href="/{i}.css">' for i in range(20))
        with test_app.test_request_context("/"):
            resp = Response(body, mimetype="text/html")
            add_link_headers(resp)
        preloads = [h for h in resp.headers.getlist("Link") if "rel=preload" in h]
        assert len(preloads) == _MAX_PRELOADS
