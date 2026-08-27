"""
公开法律 + 支持页面：`/privacy` / `/terms` / `/support`。

设计原则
--------
- **不需要登录**：放在 admin 鉴权之外，App Store 审核员 / 任何用户直接能访问
- **不继承 base.html**：base.html 带侧边栏 / CSRF 上下文，对公开页过度
- **中英双语**：`?lang=en|zh` 切换，默认跟随 `app.i18n.get_lang()`（cookie/默认中文）
- **内容来源单一**：`app/legal/*.txt` 为 canonical source of truth，三端通过 API 获取

挂载的 endpoint
- GET /privacy → privacy_page（公开）
- GET /terms   → terms_page（公开）
- GET /support → support_page（公开；App Store Connect 提交需填的 Support URL）
"""
from __future__ import annotations

from flask import Flask, Response, render_template

from app.i18n import get_lang
from app.legal import get_legal
from support_text import CONTACT_EMAIL, SECTIONS_EN, SECTIONS_ZH


def _render_legal(*, kind: str):
    """渲染法务页。`kind` ∈ {'privacy', 'terms'}。"""
    lang = get_lang()
    is_zh = lang == "zh"
    legal = get_legal(lang)

    if kind == "privacy":
        content = legal["privacy"]
        page_title = "隐私政策" if is_zh else "Privacy Policy"
        other_title = "使用条款" if is_zh else "Terms of Use"
        other_url = "/terms"
    else:  # terms
        content = legal["terms"]
        page_title = "使用条款" if is_zh else "Terms of Use"
        other_title = "隐私政策" if is_zh else "Privacy Policy"
        other_url = "/privacy"

    return render_template(
        "legal.html",
        lang=lang,
        page_title=page_title,
        content=content,
        other_title=other_title,
        other_url=other_url,
    )


def privacy_page():
    return _render_legal(kind="privacy")


def terms_page():
    return _render_legal(kind="terms")


def support_page():
    """App Store Connect 必填的 Support URL。审核员会点开验证页面真实存在。"""
    lang = get_lang()
    is_zh = lang == "zh"
    return render_template(
        "support.html",
        lang=lang,
        page_title="支持与帮助" if is_zh else "Support & Help",
        sections=(SECTIONS_ZH if is_zh else SECTIONS_EN),
        contact_email=CONTACT_EMAIL,
    )


def donate_page():
    """
    赞赏 / 打赏页。完全公开，无需登录。
    GitHub FUNDING.yml 的 custom URL 指向这里；用户点 Sponsor 按钮 → ASC
    审核员 / 任何用户都可以直接打开。

    QR 图片放 ``static/donate-alipay.{png|jpg|jpeg|webp}``，微信同理。
    自动按优先级探测；都不存在时模板显示 placeholder（管理员重传后即恢复）。
    """
    from pathlib import Path
    from config import BASE_DIR

    lang = get_lang()
    is_zh = lang == "zh"
    static_dir = Path(BASE_DIR) / "static"

    def _find_qr(stem: str) -> str | None:
        """按 png → jpg → jpeg → webp 顺序探测，返回相对 URL 或 None。"""
        for ext in ("png", "jpg", "jpeg", "webp"):
            if (static_dir / f"{stem}.{ext}").exists():
                return f"/static/{stem}.{ext}"
        return None

    return render_template(
        "donate.html",
        lang=lang,
        page_title="赞助开发者" if is_zh else "Support the developer",
        alipay_url=_find_qr("donate-alipay"),
        wechat_url=_find_qr("donate-wechat"),
        github_sponsor_url="https://github.com/sponsors/751K",
    )


#: /guide 的分享文案。正文在 docs/guide*.html 里，那份文件没有 <meta description>，
#: 所以这两行是它对外的唯一描述。
_GUIDE_META = {
    "zh": ("FlatRadar 使用指南",
           "怎么设置筛选条件、怎么接通知、自动预订是怎么工作的——FlatRadar 的完整使用说明。"),
    "en": ("FlatRadar User Guide",
           "How to set up filters, receive alerts and use auto-booking — the complete "
           "FlatRadar user guide."),
}


def guide_page():
    """用户指南——公开，无需登录。

    docs/guide*.html 是**独立的静态文件**，不走 Jinja 继承，所以拿不到
    templates/_social_meta.html 那一份分享卡片与 canonical。它却同样在 sitemap
    里，也同样会被贴进聊天窗口，缺了就和其它公开页不一致。

    这里的做法是把那个 partial 单独渲染出来，字符串注入到 ``</head>`` 之前——
    **不是**把整份文档交给 Jinja 渲染：指南正文里出现 ``{{`` 或 ``{%`` 的话会被
    当成模板语法，轻则报错重则被静默吃掉。
    """
    from config import BASE_DIR
    lang = get_lang()
    filename = "guide_cn.html" if lang == "zh" else "guide.html"
    html = (BASE_DIR / "docs" / filename).read_text(encoding="utf-8")

    title, desc = _GUIDE_META["zh" if lang == "zh" else "en"]
    meta = render_template("_social_meta.html", social_title=title, social_desc=desc)
    html = html.replace("</head>", f"{meta}\n</head>", 1)
    return Response(html, mimetype="text/html")


def register(app: Flask) -> None:
    app.add_url_rule("/privacy", endpoint="privacy_page", view_func=privacy_page, methods=["GET"])
    app.add_url_rule("/terms",   endpoint="terms_page",   view_func=terms_page,   methods=["GET"])
    app.add_url_rule("/support", endpoint="support_page", view_func=support_page, methods=["GET"])
    app.add_url_rule("/donate",  endpoint="donate_page",  view_func=donate_page,  methods=["GET"])
    app.add_url_rule("/guide",   endpoint="guide_page",   view_func=guide_page,   methods=["GET"])
