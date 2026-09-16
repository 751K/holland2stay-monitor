"""Universal Link 的两半：AASA 文件 + ``/l/<id>`` 落地页。

为什么这些断言值得写死
--------------------
Universal Link 失效时**系统一声不吭**——链接只是"在浏览器里打开了"，和没配过
一模一样。没有报错、没有日志，客户端也看不出区别。所以这条链路上每一个
"必须是这样"的细节都只能靠测试守住：

- Content-Type 写错（比如 Flask 默认的 text/html）→ 静默失效
- 路径写成 ``/*`` 而不是 ``/l/*`` → 用户在浏览器里点站内任何链接都被拽进 app
- 落地页哪天挂上 ``login_required`` → "没装 app 的人也能看"这一半就没了
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest


@pytest.fixture
def seeded(test_app):
    """一条房源就够。

    不复用 ``test_api_v1_endpoints`` 里那个 ``seeded``：那个连坐标缓存和通知
    一起塞，是给接口测试用的；这一页只读 listings 一张表，多塞的东西只会让
    这里的失败更难读。
    """
    from app.db import storage
    now = datetime.now(timezone.utc).isoformat()
    st = storage()
    st.conn.execute(
        "INSERT INTO listings (id,name,status,price_raw,available_from,"
        "features,url,city,first_seen,last_seen,last_status,source) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("id-1", "Studio Centrum", "Available to book", "€700", "2026-06-01",
         json.dumps(["Type: Studio", "Area: 26.0 m²"]),
         "https://h.com/id-1", "Eindhoven", now, now, "Available to book",
         "holland2stay"),
    )
    st.conn.commit()
    return st


class TestAppleAppSiteAssociation:

    @pytest.mark.parametrize("path", [
        "/.well-known/apple-app-site-association",
        "/apple-app-site-association",
    ])
    def test_served_at_both_paths(self, client, path):
        """新系统查 .well-known，老系统查根路径，两个都得有。"""
        r = client.get(path)
        assert r.status_code == 200

    def test_content_type_is_json(self, client):
        """Apple 的 CDN 只认 application/json。给 text/html 会被直接丢掉。"""
        r = client.get("/.well-known/apple-app-site-association")
        assert r.mimetype == "application/json"

    def test_not_a_redirect(self, client):
        """抓这个文件时不跟 301/302。重定向 = 拿不到 = 静默失效。"""
        r = client.get("/.well-known/apple-app-site-association")
        assert r.status_code == 200, "必须直接 200，不能是 3xx"

    def test_anonymous_can_fetch_it(self, client):
        """`client` 就是未登录的。挂上鉴权的话 Apple 永远抓不到。"""
        r = client.get("/.well-known/apple-app-site-association")
        assert r.status_code == 200

    def test_app_id_carries_team_prefix(self, client):
        """appIDs 的格式是 `<TeamID>.<BundleID>`，少了 team 前缀不生效。"""
        body = json.loads(client.get("/.well-known/apple-app-site-association").data)
        app_ids = body["applinks"]["details"][0]["appIDs"]
        assert len(app_ids) == 1
        team, _, bundle = app_ids[0].partition(".")
        assert team and team.isalnum() and team.isupper(), f"team 前缀不对: {app_ids[0]}"
        assert bundle == "com.j.kong.FlatRadar"

    def test_only_the_share_path_is_claimed(self, client):
        """**只认领 /l/***。

        写 `/*` 的后果是用户在浏览器里点站内任何链接（/settings、/stats…）
        都会被拽进 app 里——那是 Universal Link 最常见也最烦人的配错。
        """
        body = json.loads(client.get("/.well-known/apple-app-site-association").data)
        paths = [c["/"] for c in body["applinks"]["details"][0]["components"]]
        assert paths == ["/l/*"]

    def test_both_paths_serve_identical_content(self, client):
        a = client.get("/.well-known/apple-app-site-association").data
        b = client.get("/apple-app-site-association").data
        assert a == b


class TestShareLandingPage:

    def test_anonymous_gets_the_listing(self, client, seeded):
        """匿名可访问是这一页存在的前提，见 app/routes/share.py。"""
        r = client.get("/l/id-1")
        assert r.status_code == 200
        body = r.data.decode()
        assert "Studio Centrum" in body
        assert "€700" in body
        assert "Eindhoven" in body

    def test_shows_the_feature_fields(self, client, seeded):
        """Area / Type 这些从 features 里解出来的字段要真的出现在页面上。

        **这一条是补的，因为上面那几条没抓住一个真 bug**：
        ``parse_features_list`` 出来的键是小写的（``"Type: Studio"`` →
        ``{"type": "Studio"}``），而第一版按 ``"Area"`` / ``"Type"`` 原样大小写取，
        四个字段全空。页面照样 200、照样有名字有价格，只是少了一半内容——
        原来那几条断言一条都没红。
        """
        body = client.get("/l/id-1").data.decode()
        assert "Studio" in body, "Type 没渲染出来"
        assert "26.0 m²" in body, "Area 没渲染出来"

    def test_links_out_to_the_platform(self, client, seeded):
        """页面的主出口是平台原页——这一页是镜像，不是房源的家。"""
        body = client.get("/l/id-1").data.decode()
        assert "https://h.com/id-1" in body

    def test_missing_listing_is_gone_not_not_found(self, client, seeded):
        """410 而不是 404：房源下架的语义正是"曾经有效、现在没了"。

        搜索引擎见 410 直接删索引，见 404 还会回来重试几次——对一个注定会消失
        的页面来说，410 是准确的那个。
        """
        r = client.get("/l/does-not-exist")
        assert r.status_code == 410
        assert "gone" in r.data.decode().lower()

    def test_gone_page_still_renders_something_useful(self, client, seeded):
        """410 也要是一个人看得懂的页面，不是 Flask 的默认错误页。"""
        body = client.get("/l/does-not-exist").data.decode()
        assert "FlatRadar" in body
        assert "does-not-exist" in body

    def test_has_social_preview_tags(self, client, seeded):
        """这一页的全部用途就是被贴进 iMessage / Slack，预览卡不能是空的。"""
        body = client.get("/l/id-1").data.decode()
        assert 'property="og:title"' in body
        assert "Studio Centrum" in body

    def test_says_the_platform_is_the_source_of_truth(self, client, seeded):
        """镜像页必须说清自己是镜像。平台随时改价改状态，我们有抓取间隔。"""
        body = client.get("/l/id-1").data.decode().lower()
        assert "source of truth" in body

    def test_robots_keeps_share_pages_out_of_the_index(self, client):
        """房源会消失，今天 200 的页面下个月就是 410——不该进索引。

        拦的是爬虫；iMessage / Slack 那类抓 OpenGraph 的预览器不读 robots.txt，
        分享出去照样有卡片（上面那条测的就是卡片还在）。
        """
        body = client.get("/robots.txt").data.decode()
        assert "Disallow: /l/" in body
