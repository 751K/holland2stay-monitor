"""Early Hints（HTTP 103）的原料：给 HTML 响应加 ``Link:`` 头。
=============================================================

Cloudflare 的实现方式是**从源站 200 响应里抄走 ``Link:`` 头缓存起来，下次同一
路径先回一个 103 把它们播出去**。所以源站不发 ``Link:``，边缘就什么都播不出来
——2026-08-27 实测，这个 zone 的 ``early_hints`` 开着，而源站一个 ``Link:`` 都
没有：功能空转，只在分析里制造噪音。

省下来的是什么
--------------
浏览器发出 GET 之后、HTML 回来之前那段时间是纯干等——它不知道接下来需要什么，
因为那写在还没到的 HTML 里。103 把「接下来要用这几个资源」提前告诉它：

    GET /login ──▶
              ◀── 103  Link: </static/design.css>; rel=preload
    GET design.css ──▶        ← 服务器这时还在渲染
              ◀── 200  HTML

实测源站耗时 /login 179ms、首页 250ms，这就是能抢回来的量级。收益主要落在跨域
的第三方域名上（fonts.googleapis.com / cdn.jsdelivr.net 的 DNS + TLS 握手），
HTML 里的 ``preconnect`` 要等 HTML 到了才开始，103 能把它提前一个渲染周期。

为什么读正文，而不是按 endpoint 写一张表
----------------------------------------
各页面加载的样式表并不一致：``base.html`` 与 ``login.html`` 有 design.css 和
bootstrap-icons，而 donate / support / legal 刻意不引 design.css（各自内联），
连 Google Fonts 的字重参数都不同（``400;450;500;550;600;700`` vs
``400;500;600;700``）。发一份统一的 ``Link:`` 会让一半页面预载自己不用的东西
——浏览器会下载它、占带宽、再在控制台报 "preloaded but not used"。

按 endpoint 维护一张对照表能解决，但它和模板是两份真相，改模板时没有任何机制
逼你同步。这里改成**从渲染结果里读出这一页真正引用了哪些样式表**：模板怎么变，
``Link:`` 就怎么变，不会脱节。代价是一次字符串扫描，对几十 KB 的页面是微秒级。

刻意不预载的东西
----------------
**首屏 logo。** 它是 LCP 候选，看起来最该预载，但深浅色是两个文件，而主题由
``base.html`` 里的内联脚本在运行时才决定。写死任何一张，都必然有一半用户预载
错的那张——这正是 base.html 不把它写进 HTML、改由那段脚本动态插入的原因。
``Link:`` 头比 HTML 更早，更没有条件判断的余地。

**字体文件本身。** 真正的 woff2 URL 藏在 Google 返回的那份 CSS 里，源站不知道。
对 fonts.gstatic.com 用 ``preconnect`` 是能做的最好一步：握手先建好，CSS 一到
就能直接发请求。
"""
from __future__ import annotations

import re
from flask import Flask, Response

#: 跨域字体域名。用 preconnect 而不是 preload——见模块 docstring。
#: gstatic 那条必须带 crossorigin：字体请求是匿名 CORS 的，不带的话浏览器会
#: 另开一条连接，preconnect 白做。
_PRECONNECT = (
    "<https://fonts.googleapis.com>; rel=preconnect",
    "<https://fonts.gstatic.com>; rel=preconnect; crossorigin",
)

#: 单页最多播几条 preload。上限存在的理由不是头部大小，而是**Early Hints 抢的是
#: 带宽**：列太多会和 HTML 本身争抢，首屏反而更慢。六条足够覆盖 design.css +
#: 两个 CDN 样式表，还留了余量。
_MAX_PRELOADS = 6

#: rel 与 href 的先后不固定：base.html 写 ``rel`` 在前，donate.html 写 ``href``
#: 在前。所以先抓整个 <link> 标签，再在标签内部分别找，不能用一条带顺序的正则。
_LINK_TAG_RE = re.compile(rb'<link\b[^>]*rel="stylesheet"[^>]*>', re.I)
_HREF_RE = re.compile(rb'href="([^"]+)"', re.I)


def _stylesheets(body: bytes) -> list[str]:
    """按出现顺序取出页面真正引用的样式表 URL。"""
    out: list[str] = []
    for tag in _LINK_TAG_RE.finditer(body):
        href = _HREF_RE.search(tag.group(0))
        if not href:
            continue
        url = href.group(1).decode("utf-8", "replace")
        # HTML 实体：href 里的 & 会被 Jinja 转义成 &amp;，直接放进 Link 头会
        # 指向一个不存在的 URL（Google Fonts 的 css2?family=…&display=swap 正好
        # 每个都中招）。
        url = url.replace("&amp;", "&")
        if url not in out:
            out.append(url)
    return out


def add_link_headers(resp: Response) -> Response:
    """给 HTML 响应补 ``Link:``，作为 Cloudflare 生成 103 的原料。"""
    # 只处理成功返回的 HTML。302 到登录页的响应也带 Link 没有意义——那个路径的
    # 资源要由重定向目标自己声明。
    if resp.status_code != 200 or resp.mimetype != "text/html":
        return resp
    # 流式响应（SSE）不能碰：get_data() 会把生成器抽干，连接当场变成空响应。
    if resp.is_streamed or resp.direct_passthrough:
        return resp

    hints = list(_PRECONNECT)
    for url in _stylesheets(resp.get_data())[:_MAX_PRELOADS]:
        hints.append(f"<{url}>; rel=preload; as=style")

    # 用 add 而不是覆盖：视图自己已经声明过 Link 的话（目前没有，但这是响应头
    # 的公共约定）不该被吞掉。
    for h in hints:
        resp.headers.add("Link", h)
    return resp


def register(app: Flask) -> None:
    app.after_request(add_link_headers)
