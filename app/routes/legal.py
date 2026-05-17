"""
公开法律 + 支持页面：`/privacy` / `/terms` / `/support`。

设计原则
--------
- **不需要登录**：放在 admin 鉴权之外，App Store 审核员 / 任何用户直接能访问
- **不继承 base.html**：base.html 带侧边栏 / CSRF 上下文，对公开页过度
- **中英双语**：`?lang=en|zh` 切换，默认跟随 `app.i18n.get_lang()`（cookie/默认中文）
- **内容来源单一**：`legal_text.py` / `support_text.py` 与 iOS 对应文件保持平行

挂载的 endpoint
- GET /privacy → privacy_page（公开）
- GET /terms   → terms_page（公开）
- GET /support → support_page（公开；App Store Connect 提交需填的 Support URL）
"""
from __future__ import annotations

from flask import Flask, render_template

from app.i18n import get_lang
from legal_text import PRIVACY_EN, PRIVACY_ZH, TERMS_EN, TERMS_ZH
from support_text import CONTACT_EMAIL, SECTIONS_EN, SECTIONS_ZH


def _render_legal(*, kind: str):
    """渲染法务页。`kind` ∈ {'privacy', 'terms'}。"""
    lang = get_lang()
    is_zh = lang == "zh"

    if kind == "privacy":
        content = PRIVACY_ZH if is_zh else PRIVACY_EN
        page_title = "隐私政策" if is_zh else "Privacy Policy"
        other_title = "使用条款" if is_zh else "Terms of Use"
        other_url = "/terms"
    else:  # terms
        content = TERMS_ZH if is_zh else TERMS_EN
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


def register(app: Flask) -> None:
    app.add_url_rule("/privacy", endpoint="privacy_page", view_func=privacy_page, methods=["GET"])
    app.add_url_rule("/terms",   endpoint="terms_page",   view_func=terms_page,   methods=["GET"])
    app.add_url_rule("/support", endpoint="support_page", view_func=support_page, methods=["GET"])
