"""分享落地页：``/l/<listing_id>``。

这个页面存在的唯一理由是 Universal Link
--------------------------------------
客户端分享一套房源时发的就是 ``https://<host>/l/<id>``。装了 FlatRadar 的人点它，
系统直接把链接交给 app（靠 ``/.well-known/apple-app-site-association``，见
``app/routes/site_meta.py``）；**没装的人落到这一页**。

所以这一页的职责很窄：告诉收件人"这是哪套房、多少钱、在哪儿"，给一个去平台
原页的出口，再给一个装 app 的出口。它不是房源详情页的网页版，也不该长成那样。

为什么必须匿名可访问
------------------
链接是发给**别人**的，那个人多半没有账号。要登录才能看的话，Universal Link 的
另一半（"没装 app 的人也能看"）就不成立了——整件事退化回"只有装了 app 的人点得开"。
和 ``/privacy`` / ``/support`` 同一类：不挂 ``login_required``。

**公开的只有这一条房源，而且只有平台本来就公开的那些字段。** 这里不列"谁在关注
它"、不列通知记录、不接受任何查询参数去遍历——没有 ``/l/`` 列表页，拿不到 id 就
什么也看不到。id 是平台自己的房源编号，本来就印在平台的公开页面上。

不继承 base.html
---------------
和 ``app/routes/legal.py`` 同一个理由：base.html 带侧边栏和 CSRF 上下文，
对一个公开的单页过度。这里用独立模板。
"""
from __future__ import annotations

from flask import Flask, Response, render_template

from app.i18n import get_lang
from app.services.listing_service import get_listing_detail, serialize_listing


def _status_tone(status: str) -> str:
    """状态映射到模板里的一个 class 名。

    口径跟客户端的 ``ListingStatus`` 走：能订的是绿、抽签是橙、已预订是蓝、
    占用是灰。归一在客户端是 `ListingStatus.from`，这里只需要粗分四档，
    所以就地判断，不把那整套枚举搬过来。
    """
    s = (status or "").lower()
    if "direct" in s or "available" in s or "book" in s:
        return "ok"
    if "lottery" in s or "reacting" in s:
        return "warn"
    if "reserved" in s:
        return "info"
    return "muted"


def listing_share_page(listing_id: str):
    """``GET /l/<listing_id>``。

    房源不存在时给 **410 Gone**，不是 404。
    ``410`` 说的是"这个地址曾经有效、现在没了"，而房源下架正是这个意思——
    搜索引擎见 410 会直接删索引，见 404 还会回来重试几次。对一个注定会消失的
    页面来说，410 是准确的那个。
    """
    lang = get_lang()
    row = get_listing_detail(listing_id, None)
    if row is None:
        html = render_template("share_listing.html",
                               lang=lang,
                               listing=None,
                               listing_id=listing_id)
        return Response(html, status=410, mimetype="text/html")

    data = serialize_listing(row)
    feature_map = data.get("feature_map") or {}

    def feat(*keys: str) -> str:
        """按候选键取第一个非空的。

        ``parse_features_list`` 出来的键是**小写**的（``"Type: 2-room"`` →
        ``{"type": "2-room"}``）。第一版按 ``"Area"`` / ``"Energy label"`` 这样的
        原样大小写取，结果四个字段全是空的——页面能渲染，只是少了一半内容，
        没有任何报错。

        候选键的清单和客户端 ``Listing.areaText`` 那几个 accessor 保持一致：
        各平台的字段名不统一（Area / Surface、Energy / Energy label），
        两边认同一组才不会出现"app 里看得见、分享页上没有"。
        """
        for k in keys:
            v = feature_map.get(k)
            if v:
                return str(v)
        return ""

    return render_template(
        "share_listing.html",
        lang=lang,
        listing=data,
        listing_id=listing_id,
        # 模板不做逻辑：要显示什么在这儿算好。
        area=feat("area", "surface", "living area", "m2", "m²"),
        room_type=feat("type", "property type", "apartment type"),
        building=feat("building", "building name", "building_name", "complex"),
        energy=feat("energy", "energy label"),
        tone=_status_tone(data.get("status", "")),
    )


def register(app: Flask) -> None:
    app.add_url_rule("/l/<string:listing_id>", endpoint="listing_share_page",
                     view_func=listing_share_page, methods=["GET"])
