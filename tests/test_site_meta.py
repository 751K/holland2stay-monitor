"""``/robots.txt`` 与根路径图标。

这些是浏览器/爬虫**在解析 HTML 之前**就按约定向根路径发的请求，模板里的
``<link rel>`` 救不了。2026-08-24 从 Caddy 日志实测的 20 天窗口：

    /robots.txt                    307 次   **全部 404，一次都没成功过**
    /favicon.ico                    87 次
    /apple-touch-icon.png           14 次
    /apple-touch-icon-precomposed   14 次
"""
from __future__ import annotations

import pytest

from app.routes.site_meta import (
    _DISALLOW,
    _DISALLOW_NEEDS_AUTH,
    _DISALLOW_SENSITIVE,
)


class TestRobotsTxt:
    def test_served_without_login(self, client):
        r = client.get("/robots.txt")
        assert r.status_code == 200
        assert r.mimetype == "text/plain"

    def test_disallows_the_api(self):
        """日志里 Googlebot 确实在爬 /api/status —— 爬了也只拿到 401。"""
        assert "/api/" in _DISALLOW

    def test_auth_group_really_needs_auth(self, client):
        """第一类的判据是「爬了也拿不到内容」。

        哪条路径哪天变公开了，这里要能发现——写这组用例时它就抓到过一次：
        ``/login`` 本来在名单里，实际对匿名访客返回 200，属于第二类都不是的
        纯公开页，已移出。
        """
        for path in _DISALLOW_NEEDS_AUTH:
            r = client.get(path)
            assert r.status_code in (301, 302, 401, 403, 404, 405), (
                f"{path} 归在「需要登录」组，却对未登录访客返回 {r.status_code}"
                "——要么它其实是公开内容不该拦，要么权限漏了"
            )

    def test_sensitive_group_is_reachable_but_hidden(self, client):
        """第二类恰恰是**能访问**的，拦它是因为不该进搜索引擎。

        别拿第一类的判据去要求它，否则这条规则会被「修」掉。
        """
        assert _DISALLOW_SENSITIVE, "第二类为空的话这条用例就是摆设"
        r = client.get("/verify-email/some-token")
        assert r.status_code == 200, "它是公开可达的；不可达的话就不需要 Disallow 了"

    def test_login_is_not_disallowed(self):
        """公开页面不该被拦——它对匿名访客返回 200，且是落地页的去处。"""
        assert not any(p.startswith("/login") for p in _DISALLOW)

    def test_public_pages_are_not_disallowed(self):
        """落地页与法务/赞助页必须可被索引，否则站点等于从搜索里消失。"""
        for public in ("/", "/privacy", "/terms", "/support", "/donate", "/guide"):
            for rule in _DISALLOW:
                if public == "/":
                    assert rule != "/", "Disallow: / 会屏蔽整站"
                    continue
                assert not public.startswith(rule), (
                    f"公开页 {public} 命中了 Disallow: {rule}"
                )

    def test_does_not_duplicate_cloudflare_managed_rules(self):
        """AI 爬虫的 Disallow 由 Cloudflare Managed robots.txt 提供。

        CF 的做法是回源取本文件再把托管块拼上去，所以这里重复写一遍只会让
        同一份 robots.txt 出现两组同名 User-agent，改动时两边还容易失配。
        """
        from app.routes.site_meta import _ROBOTS
        for bot in ("ClaudeBot", "GPTBot", "CCBot", "Bytespider",
                    "Google-Extended", "Amazonbot", "Applebot-Extended"):
            assert bot not in _ROBOTS, f"{bot} 该由 Cloudflare 托管块负责"

    def test_has_exactly_one_user_agent_group(self):
        from app.routes.site_meta import _ROBOTS
        assert _ROBOTS.count("User-agent:") == 1


class TestRootIcons:
    @pytest.mark.parametrize("path,target", [
        ("/favicon.ico", "/static/favicon.png"),
        ("/apple-touch-icon.png", "/static/apple-touch-icon.png"),
        ("/apple-touch-icon-precomposed.png", "/static/apple-touch-icon.png"),
    ])
    def test_redirects_to_the_real_file(self, client, path, target):
        r = client.get(path)
        assert r.status_code == 301, "用 301 让浏览器/CDN 把结果缓存到真实 URL"
        assert r.headers["Location"].endswith(target)

    @pytest.mark.parametrize("target", [
        "/static/favicon.png", "/static/apple-touch-icon.png",
    ])
    def test_redirect_target_exists(self, client, target):
        """重定向到一个不存在的文件，只是把 404 换了个地方。"""
        r = client.get(target)
        assert r.status_code == 200
        assert r.mimetype.startswith("image/")

    def test_available_without_login(self, client):
        """图标要在登录页上就能显示。"""
        for p in ("/favicon.ico", "/apple-touch-icon.png"):
            assert client.get(p).status_code == 301
