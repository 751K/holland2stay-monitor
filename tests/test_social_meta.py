"""分享卡片、规范化链接、语言协商、``/llms.txt``。

为什么这几件事写在一个文件里
----------------------------
它们是同一个问题的四个面：**站点被别人看见时，看见的是什么**。
2026-08-27 实测下来，三处全是空的：

- ``og:`` / ``twitter:`` 一个都没有 —— 链接贴进 WhatsApp / Teams / Telegram
  只显示一行光秃秃的网址。而那 5 天里 343 个独立访客有 220 个直接落在 ``/``
  且不带 referer，正是被人传人带来的那批。
- 不带 ``Accept-Language`` 的客户端（Googlebot 就是这么爬的）拿到中文页 ——
  整个站在被当成中文站收录。
- 没有 ``canonical``：``/`` 302 到 ``/login?next=/``，每个 next 取值都是一份
  重复内容。
"""
from __future__ import annotations

import re

import pytest

from app.routes.site_meta import _PUBLIC_ENDPOINTS

#: 与 sitemap 同源的公开页清单。判据也一样：**匿名访客真的能拿到 200**。
#: 写死一份 path 而不是 url_for，是为了让「某天某个页面悄悄要登录了」这件事
#: 在这里也断得出来，而不是跟着路由一起改。
PUBLIC_PATHS = ("/login", "/guide", "/donate", "/support", "/privacy", "/terms")

BASE = "https://flatradar.test"


@pytest.fixture
def pub(client, monkeypatch):
    """固定 PUBLIC_BASE_URL，让断言可以直接比较绝对 URL。"""
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    return client


def _meta(body: str, attr: str, key: str) -> list[str]:
    return re.findall(
        rf'<meta[^>]*\b{attr}="{re.escape(key)}"[^>]*\bcontent="([^"]*)"', body)


def _link(body: str, rel: str) -> list[str]:
    return re.findall(rf'<link[^>]*\brel="{rel}"[^>]*\bhref="([^"]*)"', body)


class TestPublicPathsMatchSitemap:
    def test_covers_every_sitemap_endpoint(self, pub):
        """本文件的清单漏一个页面，那个页面就再也没人检查它的卡片。"""
        from flask import url_for
        with pub.application.test_request_context():
            expected = {url_for(ep) for ep in _PUBLIC_ENDPOINTS}
        assert expected == set(PUBLIC_PATHS)

    @pytest.mark.parametrize("path", PUBLIC_PATHS)
    def test_reachable_anonymously(self, pub, path):
        assert pub.get(path).status_code == 200


class TestShareCard:
    @pytest.mark.parametrize("path", PUBLIC_PATHS)
    @pytest.mark.parametrize("prop", [
        "og:type", "og:site_name", "og:title", "og:description",
        "og:url", "og:image", "og:locale",
    ])
    def test_open_graph_present_and_nonempty(self, pub, path, prop):
        body = pub.get(path).get_data(as_text=True)
        got = _meta(body, "property", prop)
        assert got, f"{path} 缺 {prop}"
        assert got[0].strip(), f"{path} 的 {prop} 是空的——渲染出空 content 比没有更糟"

    @pytest.mark.parametrize("path", PUBLIC_PATHS)
    @pytest.mark.parametrize("name", [
        "twitter:card", "twitter:title", "twitter:description", "twitter:image",
    ])
    def test_twitter_present_and_nonempty(self, pub, path, name):
        body = pub.get(path).get_data(as_text=True)
        got = _meta(body, "name", name)
        assert got and got[0].strip(), f"{path} 缺 {name}"

    @pytest.mark.parametrize("path", PUBLIC_PATHS)
    def test_large_image_card(self, pub, path):
        """``summary`` 是 120px 的小方图，``summary_large_image`` 才是大卡。

        这张 1200×630 是按大卡画的，声明成小卡等于白画。
        """
        body = pub.get(path).get_data(as_text=True)
        assert _meta(body, "name", "twitter:card") == ["summary_large_image"]

    @pytest.mark.parametrize("path", PUBLIC_PATHS)
    def test_description_has_exactly_one_source(self, pub, path):
        """``<meta name="description">`` 只能来自 _social_meta.html。

        调用方自己再写一份的话，og:description 和 description 会各说各话，
        而改动时几乎一定只改一边。
        """
        body = pub.get(path).get_data(as_text=True)
        assert len(_meta(body, "name", "description")) == 1

    @pytest.mark.parametrize("path", PUBLIC_PATHS)
    def test_description_matches_og(self, pub, path):
        body = pub.get(path).get_data(as_text=True)
        assert _meta(body, "name", "description") == _meta(body, "property", "og:description")

    def test_title_matches_the_page_title(self, pub):
        """og:title 与 <title> 必须是同一句。

        写成两份只会各自漂移：改了搜索标题、忘了改分享卡片，两个入口从此说
        不一样的话。tests/test_landing_seo.py 那条「login_title 只用于标题」
        的规则因此显式放行 og:title 复用它。
        """
        body = pub.get("/login").get_data(as_text=True)
        title = re.search(r"<title>([^<]*)</title>", body).group(1)
        assert _meta(body, "property", "og:title") == [title]
        assert _meta(body, "name", "twitter:title") == [title]

    @pytest.mark.parametrize("path", PUBLIC_PATHS)
    def test_image_is_absolute_https(self, pub, path):
        """og:image 必须绝对：抓取方不在本站上下文里，相对路径它解不开。"""
        body = pub.get(path).get_data(as_text=True)
        assert _meta(body, "property", "og:image") == [f"{BASE}/static/og-card.png"]

    def test_image_file_exists_and_is_the_declared_size(self, pub):
        """声明 1200×630 却给一张别的尺寸，部分平台会直接拒绝渲染。"""
        r = pub.get("/static/og-card.png")
        assert r.status_code == 200 and r.mimetype == "image/png"

        import struct
        w, h = struct.unpack(">II", r.get_data()[16:24])
        body = pub.get("/login").get_data(as_text=True)
        assert [str(w), str(h)] == [
            _meta(body, "property", "og:image:width")[0],
            _meta(body, "property", "og:image:height")[0],
        ]
        assert (w, h) == (1200, 630), "og:image 的事实标准比例是 1.91:1"

    def test_image_needs_no_login(self, client):
        """抓取方没有 session。图片要登录的话，卡片对所有人都是空的。"""
        assert client.get("/static/og-card.png").status_code == 200


class TestCanonical:
    @pytest.mark.parametrize("path", PUBLIC_PATHS)
    def test_self_referencing_and_absolute(self, pub, path):
        body = pub.get(path).get_data(as_text=True)
        assert _link(body, "canonical") == [f"{BASE}{path}"]

    @pytest.mark.parametrize("path", PUBLIC_PATHS)
    def test_og_url_agrees_with_canonical(self, pub, path):
        body = pub.get(path).get_data(as_text=True)
        assert _meta(body, "property", "og:url") == _link(body, "canonical")

    def test_drops_query_noise(self, pub):
        """``/`` 对匿名访客 302 到 ``/login?next=/``，爬虫顺着过来就是这个 URL。

        不收敛的话，每一个 next 取值都是一份重复内容。
        """
        body = pub.get("/login?next=/listings&foo=bar").get_data(as_text=True)
        assert _link(body, "canonical") == [f"{BASE}/login"]

    def test_keeps_explicit_lang(self, pub):
        """``?lang=`` 是**内容真的不同**的那种参数，不能和 next 一样丢掉。

        丢掉的话两个语言版本会共用一个 canonical，等于告诉搜索引擎只索引一个。
        """
        body = pub.get("/login?lang=zh").get_data(as_text=True)
        assert _link(body, "canonical") == [f"{BASE}/login?lang=zh"]

    def test_ignores_a_bogus_lang(self, pub):
        """``?lang=klingon`` 渲染出来的其实是协商语言，canonical 不该假装它是一版。"""
        body = pub.get("/login?lang=klingon").get_data(as_text=True)
        assert _link(body, "canonical") == [f"{BASE}/login"]

    def test_host_is_not_hardcoded(self, client, monkeypatch):
        """自部署的人换域名不该改代码——和 sitemap 用同一条判据。"""
        monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
        body = client.get("/login", base_url="https://example.test").get_data(as_text=True)
        assert _link(body, "canonical") == ["https://example.test/login"]
        assert "flatradar.app" not in body

    def test_public_base_url_keeps_https(self, client, monkeypatch):
        """源站在 Caddy + Cloudflare 后面，到 Flask 这一跳是明文 HTTP。

        只信 request.url_root 的话 canonical 会指向 ``http://``，而搜索引擎把
        http 与 https 当两个站点——canonical 指向站外是最坏的一种写法。
        """
        monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
        body = client.get("/login", base_url="http://origin.internal").get_data(as_text=True)
        assert _link(body, "canonical") == [f"{BASE}/login"]
        assert "origin.internal" not in body


class TestHreflang:
    @pytest.mark.parametrize("path", PUBLIC_PATHS)
    def test_declares_both_languages_and_a_default(self, pub, path):
        body = pub.get(path).get_data(as_text=True)
        codes = re.findall(r'<link[^>]*rel="alternate"[^>]*hreflang="([^"]*)"', body)
        assert set(codes) == {"en", "zh", "x-default"}

    @pytest.mark.parametrize("path", PUBLIC_PATHS)
    def test_targets_point_at_the_lang_param(self, pub, path):
        body = pub.get(path).get_data(as_text=True)
        pairs = dict(re.findall(
            r'<link[^>]*rel="alternate"[^>]*hreflang="([^"]*)"[^>]*href="([^"]*)"', body))
        assert pairs["en"] == f"{BASE}{path}?lang=en"
        assert pairs["zh"] == f"{BASE}{path}?lang=zh"
        assert pairs["x-default"] == f"{BASE}{path}"

    @pytest.mark.parametrize("code,expect", [("en", "en"), ("zh", "zh")])
    def test_targets_really_serve_that_language(self, pub, code, expect):
        """指向一个不生效的 URL 比不写 hreflang 更糟——搜索引擎会当成配置错误。

        判据不能只看 200：``?lang=`` 要是哪天不再被 get_lang 读，页面照样 200，
        只是语言不对。所以断言渲染出来的 ``<html lang>``。
        """
        r = pub.get(f"/login?lang={code}", headers={"Accept-Language": "de-DE"})
        assert r.status_code == 200
        assert f'<html lang="{expect}"' in r.get_data(as_text=True)

    @pytest.mark.parametrize("path", PUBLIC_PATHS)
    def test_locale_alternate_is_the_other_language(self, pub, path):
        body = pub.get(path, headers={"Accept-Language": "en-US"}).get_data(as_text=True)
        assert _meta(body, "property", "og:locale") == ["en_US"]
        assert _meta(body, "property", "og:locale:alternate") == ["zh_CN"]


class TestLanguageNegotiation:
    """这一组守的是 get_lang 的回落方向。"""

    def test_no_accept_language_falls_back_to_english(self, client):
        """Googlebot 就是不带这个头爬的。回落成 zh 等于把整站按中文站收录。"""
        r = client.get("/login", headers={"Accept-Language": ""})
        assert '<html lang="en"' in r.get_data(as_text=True)

    def test_chinese_browsers_still_get_chinese(self, client):
        """改回落**只该影响不发这个头的客户端**。真人浏览器一定会发。"""
        r = client.get("/login", headers={"Accept-Language": "zh-CN,zh;q=0.9"})
        assert '<html lang="zh"' in r.get_data(as_text=True)

    def test_unknown_language_falls_back_to_english(self, client):
        r = client.get("/login", headers={"Accept-Language": "de-DE,de;q=0.9"})
        assert '<html lang="en"' in r.get_data(as_text=True)

    def test_explicit_param_beats_the_header(self, client):
        r = client.get("/login?lang=zh", headers={"Accept-Language": "en-US"})
        assert '<html lang="zh"' in r.get_data(as_text=True)

    def test_cookie_beats_the_header(self, client):
        client.set_cookie("h2s-lang", "zh")
        r = client.get("/login", headers={"Accept-Language": "en-US"})
        assert '<html lang="zh"' in r.get_data(as_text=True)


class TestVaryHeader:
    def test_html_varies_on_accept_language(self, client):
        """同一个 URL 按 Accept-Language 返回两种语言。不声明的话，任何中间缓存
        都会把第一个访客拿到的那份发给后面所有人。
        """
        vary = client.get("/login").headers.get("Vary", "")
        assert "accept-language" in vary.lower()

    def test_session_vary_survives(self, client):
        """最终响应上 Cookie 和 Accept-Language 都要在。

        **不是**靠 _add_security_headers 里的合并：session 的 ``Vary: Cookie``
        由 Flask 的 process_response 在所有 after_request 跑完之后才追加，那个
        钩子根本看不到它。这条守的是最终结果，不是那段合并逻辑——合并逻辑由下面
        那条测。
        """
        vary = client.get("/login").headers.get("Vary", "").lower()
        assert "cookie" in vary and "accept-language" in vary

    def test_merges_with_a_vary_set_earlier(self, test_app):
        """已经有人写过 Vary 时要合并，不能整条覆盖。

        这条一开始是假的：原来写的是「不要覆盖 session 的 Vary: Cookie」，而
        session 那一份根本还不存在，于是把合并逻辑改成直接赋值也照样绿。
        真要测就得**自己造一个更早的 Vary**——Flask 按注册的逆序执行
        after_request，所以这里临时注册的钩子会跑在 _add_security_headers 前面。
        """
        def _pre(resp):
            resp.headers["Vary"] = "Origin"
            return resp

        funcs = test_app.after_request_funcs.setdefault(None, [])
        funcs.append(_pre)
        try:
            vary = test_app.test_client().get("/login").headers.get("Vary", "").lower()
        finally:
            funcs.remove(_pre)
        assert "origin" in vary, "更早写好的 Vary 被整条覆盖了"
        assert "accept-language" in vary

    def test_static_is_not_varied(self, client):
        """/static/ 下的资源与语言无关，加 Vary 只会白白降低边缘缓存命中率。"""
        vary = client.get("/static/og-card.png").headers.get("Vary", "")
        assert "accept-language" not in vary.lower()

    def test_added_only_once(self, client):
        vary = client.get("/login").headers.get("Vary", "").lower()
        assert vary.count("accept-language") == 1


class TestSiteVerification:
    def test_absent_by_default(self, client, monkeypatch):
        """没配就不该渲染一个空 content 的验证标签——Search Console 会判失败。"""
        monkeypatch.delenv("GOOGLE_SITE_VERIFICATION", raising=False)
        body = client.get("/login").get_data(as_text=True)
        assert "google-site-verification" not in body

    @pytest.mark.parametrize("path", PUBLIC_PATHS)
    def test_rendered_on_every_public_page_when_set(self, client, monkeypatch, path):
        """Search Console 验证的是**某一个 URL**；只挂在首页的话，换个入口就验不过。"""
        monkeypatch.setenv("GOOGLE_SITE_VERIFICATION", "tok-123")
        body = client.get(path).get_data(as_text=True)
        assert _meta(body, "name", "google-site-verification") == ["tok-123"]

    def test_blank_is_treated_as_unset(self, client, monkeypatch):
        monkeypatch.setenv("GOOGLE_SITE_VERIFICATION", "   ")
        assert "google-site-verification" not in client.get("/login").get_data(as_text=True)


class TestLlmsTxt:
    """给 AI 检索爬虫的站点说明。

    2026-08-27 的 Caddy 日志里它们已经是抓得最勤的一批（Claude-SearchBot 204 次、
    ClaudeBot 49、PerplexityBot 12、GPTBot 14、xAI 10、DeepSeekBot 5），而抓到的
    是一张登录表单。
    """

    def test_served_as_plain_text_without_login(self, client):
        r = client.get("/llms.txt")
        assert r.status_code == 200
        assert r.mimetype == "text/plain"

    def test_has_the_required_shape(self, pub):
        body = pub.get("/llms.txt").get_data(as_text=True)
        assert body.startswith("# FlatRadar"), "llmstxt.org 要求首行是 H1"
        assert "\n> " in body, "H1 之后要有一段 blockquote 摘要"

    def test_says_it_is_unaffiliated(self, pub):
        """这一句是给会转述本站的机器读的——被 AI 说成官方渠道是真实风险。"""
        body = pub.get("/llms.txt").get_data(as_text=True).lower()
        assert "unofficial" in body and "not affiliated" in body

    def test_every_linked_page_is_anonymously_reachable(self, pub):
        """判据同 sitemap：列一个要登录的链接，抓取方只会拿到 302。"""
        body = pub.get("/llms.txt").get_data(as_text=True)
        paths = re.findall(rf"\({re.escape(BASE)}(/[^)]*)\)", body)
        assert set(paths) == set(PUBLIC_PATHS)
        for path in paths:
            assert pub.get(path).status_code == 200, f"llms.txt 列了 {path} 但它进不去"

    def test_links_are_absolute(self, pub):
        """抓取方不在本站上下文里，相对路径解不开。"""
        body = pub.get("/llms.txt").get_data(as_text=True)
        assert not re.search(r"\]\(/", body), "有相对链接"

    def test_host_is_not_hardcoded(self, client, monkeypatch):
        monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
        body = client.get("/llms.txt", base_url="https://example.test").get_data(as_text=True)
        assert "https://example.test/login" in body
        assert "flatradar.app" not in body

    def test_not_blocked_by_our_own_robots(self, client):
        from app.routes.site_meta import _DISALLOW
        assert not any("/llms.txt".startswith(rule) for rule in _DISALLOW)


class TestGuideInjection:
    """/guide 是 docs/ 下的静态文件，卡片是字符串注进去的。"""

    def test_meta_lands_inside_the_head(self, pub):
        body = pub.get("/guide").get_data(as_text=True)
        head = body.index("</head>")
        assert body.index('rel="canonical"') < head
        assert body.index('property="og:image"') < head

    def test_head_is_not_duplicated(self, pub):
        body = pub.get("/guide").get_data(as_text=True)
        assert body.count("</head>") == 1

    def test_body_is_not_template_evaluated(self, pub):
        """指南正文里若出现 ``{{`` 或 ``{%``，交给 Jinja 渲染会被吃掉或报错。

        判据是「文件里有的东西，响应里一字不差还在」——只数长度的话，正文被
        整段替换成空字符串也测不出来。
        """
        from config import BASE_DIR
        raw = (BASE_DIR / "docs" / "guide.html").read_text(encoding="utf-8")
        body = pub.get("/guide", headers={"Accept-Language": "en-US"}).get_data(as_text=True)
        marker = raw[raw.index("</head>") + len("</head>"):]
        assert marker in body, "指南正文被改写了"

    def test_language_picks_the_right_file(self, pub):
        zh = pub.get("/guide", headers={"Accept-Language": "zh-CN"}).get_data(as_text=True)
        en = pub.get("/guide", headers={"Accept-Language": "en-US"}).get_data(as_text=True)
        assert "使用指南" in zh
        assert "User Guide" in en
        assert zh != en
