"""站点元文件：``/robots.txt`` 与根路径图标。

都是**浏览器和爬虫按约定直接向根路径要**的东西，模板里的 ``<link rel>`` 救不了
——它们在解析 HTML 之前就发请求了。不提供的话每一次都是 404。

2026-08-24 从 Caddy 访问日志实测（20 天窗口）：

    /robots.txt                    307 次   全部 404，一次都没成功过
    /favicon.ico                    87 次
    /apple-touch-icon.png           14 次
    /apple-touch-icon-precomposed   14 次

robots.txt 与 Cloudflare 的关系
-------------------------------
站点前面挂着 Cloudflare，并且开了 **Managed robots.txt**。它的工作方式是
「回源取你自己的 robots.txt，再把托管块拼上去」——源站没有这个文件时，回源
拿到 404，CF 就只输出自己那一块。

所以在这次改动之前，对外可见的 robots.txt 是 100% Cloudflare 托管的：AI 训练类
爬虫（ClaudeBot / GPTBot / CCBot / Bytespider / Google-Extended …）已被
``Disallow``，搜索引擎放行。**那部分不要在这里重复**，CF 会自己拼上。

本文件只补 CF 管不到的那半：哪些**路径**不值得爬。

同一个 ``User-agent: *`` 会出现两次（这里一次、CF 托管块一次），这是允许的：
按 robots.txt 的最长匹配规则，``Disallow: /api/`` 比托管块里的 ``Allow: /``
更具体，``/api/*`` 仍然会被拒。
"""
from __future__ import annotations

import os

from flask import Flask, Response, redirect, request, url_for


#: 拦住的路径分两类，理由不同——混成一条判据的话，哪天某个路径变公开了也看不出来。

#: 一、**爬了也拿不到内容**：需要登录，爬虫只会拿到 302 / 401 / 405。
#: 日志里 Googlebot 确实在爬 ``/api/status`` 这类接口，纯属双方浪费。
_DISALLOW_NEEDS_AUTH = (
    "/api/",            # 全部接口：要么鉴权，要么是前端轮询用的
    "/logout",
    "/users",
    "/settings",
    "/monitoring",
    "/logs",
    "/crashes",
    "/system",
)

#: 二、**能访问，但不该被索引**：一次性令牌进了搜索引擎就是泄漏面。
#: 这类路径对匿名访客返回 200 是正常的，不要拿上面那条判据去要求它。
_DISALLOW_SENSITIVE = (
    "/verify-email/",
)

#: ``/login`` **刻意不拦**：它对匿名访客返回 200，是真正的公开页面，
#: 拦它属于 SEO 取舍而不是「爬了拿不到东西」，与本文件的判据不符。
#: 落地页 ``/`` 已经链过去了，被索引也没有坏处。
_DISALLOW = _DISALLOW_NEEDS_AUTH + _DISALLOW_SENSITIVE

#: sitemap 里要列的公开页。**判据是「匿名访客真的能拿到 200」**——
#: 2026-08-24 实测，除这几个之外全部 302 到登录页：
#:
#:     /login 26.9KB   /guide 17KB   /privacy 13.2KB
#:     /terms 11.4KB   /support 8.6KB   /donate 7.4KB
#:
#: 注意 ``/`` **不在其中**：它对匿名访客 302 到 ``/login``，而 sitemap 里列
#: 重定向 URL 是坏习惯，列真正返回 200 的那个。
#:
#: 也注意 **guest mode 不改变这份名单**：它是 ``POST /guest`` 的交互式开关，
#: 靠 session 生效，爬虫既不会 POST 也不带 session，看到的和匿名访客一样。
_PUBLIC_ENDPOINTS = (
    "login",
    "guide_page",
    "donate_page",
    "support_page",
    "privacy_page",
    "terms_page",
)

_ROBOTS = ("User-agent: *\n"
           + "".join(f"Disallow: {p}\n" for p in _DISALLOW))


def site_root() -> str:
    """对外可见的站点根 URL，无尾斜杠。

    优先 ``PUBLIC_BASE_URL``——项目已有的这个变量就是为此存在的，而且是**唯一
    知道真实 scheme 的来源**：源站在 Caddy + Cloudflare 后面，TLS 在边缘就终止
    了，到 Flask 这一跳是明文 HTTP，``request.url_root`` 因此返回 ``http://``。
    2026-08-24 上线后实测 sitemap 里全是 http:// —— 而搜索引擎把 http 与 https
    视作两个站点，等于整份 sitemap 都指向"站外"。

    没配时回落到 ``request.url_root``，自部署与本地开发才跑得起来。

    注意这里的回落标准**故意比 app/email_verify.py 松**：那边拒绝回落，因为
    邮件里的验证链接是安全边界，Host header 可伪造，伪造出的链接会发给真实
    用户。sitemap 不同——伪造的 Host 只会让攻击者自己拿到一份指向自己域名的
    XML，害不到别人。
    """
    base = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    return base or request.url_root.rstrip("/")


def robots_txt():
    """源站自己的 robots.txt。Cloudflare 会在其后拼上托管块。"""
    body = _ROBOTS + f"\nSitemap: {site_root()}/sitemap.xml\n"
    return Response(body, mimetype="text/plain")


def sitemap_xml():
    """列出匿名访客真正能打开的页面。

    刻意不写 ``<lastmod>`` / ``<changefreq>`` / ``<priority>``：前者没有真实的
    修改时间可依据，编一个就是撒谎；后两者主流搜索引擎早已忽略。宁可给一份短
    而准确的。
    """
    root = site_root()
    urls = "".join(
        f"  <url><loc>{root}{url_for(ep)}</loc></url>\n"
        for ep in _PUBLIC_ENDPOINTS
    )
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
           f"{urls}</urlset>\n")
    return Response(xml, mimetype="application/xml")


#: ``/llms.txt`` 的正文。格式见 https://llmstxt.org/ ——一个 H1、一段 blockquote
#: 摘要、若干「标题 + 链接列表」小节。
#:
#: 为什么值得单独出一份：2026-08-27 的 Caddy 日志里，AI 检索类爬虫已经是抓得
#: 最勤的一批（Claude-SearchBot 204 次、ClaudeBot 49、PerplexityBot 12、
#: GPTBot 14、OAI-SearchBot 7、xAI 10、DeepSeekBot 5），而 GitHub 那边已经能
#: 看到真人从 chatgpt.com 过来。它们抓到的却是一张登录表单——``/`` 对匿名访客
#: 302 到 ``/login``，正文里没有一句话说明这站是干什么的。
#:
#: 注意这里**只写公开页**，判据与 ``_PUBLIC_ENDPOINTS`` 相同：写一个要登录的
#: 链接进去，只会让抓取方拿到 302。
def _llms_txt() -> str:
    root = site_root()
    return f"""# FlatRadar

> Monitors Dutch rental platforms (Holland2Stay, Xior, OurDomain, OurCampus)
> and alerts you the moment a listing you can actually book appears. Filter by
> city, rent, floor area and tenant eligibility; notifications go to email,
> Telegram, iOS and Android push. Free to use, and self-hostable.

FlatRadar is an unofficial, independent tool. It is not affiliated with any of
the platforms it monitors, and it does not list or let properties itself.

## Pages

- [Sign in / overview]({root}/login): what the service does, and where you sign up.
- [User guide]({root}/guide): how filters, notifications and auto-booking work.
- [Support]({root}/support): contact and troubleshooting.
- [Donate]({root}/donate): how to support development.

## Legal

- [Privacy policy]({root}/privacy)
- [Terms of service]({root}/terms)

## Source

- [Source code](https://github.com/751K/holland2stay-monitor): Python, Flask, SQLite; self-hosting instructions in the README.
"""


def llms_txt():
    """``/llms.txt`` —— 给 AI 检索爬虫的站点说明。"""
    return Response(_llms_txt(), mimetype="text/plain")


#: 语言代码 -> og:locale。og 协议要的是 ``language_TERRITORY``，不是裸的 ISO 639。
_OG_LOCALE = {"zh": "zh_CN", "en": "en_US"}


def social_meta() -> dict:
    """给模板算好一份分享卡片 / 规范化链接要用的绝对 URL。

    **canonical 只用 ``request.path``，丢掉全部查询参数**（``lang`` 除外）。
    落地页尤其需要这一条：``/`` 对匿名访客 302 到 ``/login?next=/``，爬虫顺着
    过来看到的就是带 ``next=`` 的那个 URL；不收敛的话每一个 ``next`` 取值都是
    一份重复内容。

    hreflang 指向 ``?lang=xx``：``get_lang()`` 读的就是这个查询参数，所以这两个
    URL 是真能拿到对应语言的——指一个不生效的 URL 比不写还糟。``x-default``
    给不带参数的那个，交回给 Accept-Language 协商。
    """
    from app.i18n import get_lang

    root = site_root()
    base = f"{root}{request.path}"
    explicit = request.args.get("lang", "")
    lang = get_lang()
    canonical = f"{base}?lang={explicit}" if explicit in _OG_LOCALE else base
    return {
        "canonical":   canonical,
        "alternates":  (("en", f"{base}?lang=en"),
                        ("zh", f"{base}?lang=zh"),
                        ("x-default", base)),
        "image":       f"{root}/static/og-card.png",
        "locale":      _OG_LOCALE.get(lang, "en_US"),
        "locale_alt":  [v for k, v in _OG_LOCALE.items() if k != lang],
        "verification": os.environ.get("GOOGLE_SITE_VERIFICATION", "").strip(),
    }


def favicon_ico():
    """``/favicon.ico`` → 实际文件。

    用 301 而不是直接返回文件：让浏览器和 CDN 把结果缓存到真实 URL 上，
    省掉后续每次的这一跳。
    """
    return redirect("/static/favicon.png", code=301)


def apple_touch_icon():
    """iOS 添加到主屏时按约定直接要根路径的这两个名字。"""
    return redirect("/static/apple-touch-icon.png", code=301)


def register(app: Flask) -> None:
    app.context_processor(lambda: {"social": social_meta()})
    app.add_url_rule("/llms.txt", endpoint="llms_txt",
                     view_func=llms_txt, methods=["GET"])
    app.add_url_rule("/robots.txt", endpoint="robots_txt",
                     view_func=robots_txt, methods=["GET"])
    app.add_url_rule("/sitemap.xml", endpoint="sitemap_xml",
                     view_func=sitemap_xml, methods=["GET"])
    app.add_url_rule("/favicon.ico", endpoint="favicon_ico",
                     view_func=favicon_ico, methods=["GET"])
    for rule in ("/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"):
        app.add_url_rule(rule, endpoint="apple_touch_icon" + rule.replace("/", "_"),
                         view_func=apple_touch_icon, methods=["GET"])
