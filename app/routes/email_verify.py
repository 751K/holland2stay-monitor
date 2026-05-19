"""路由：收件邮箱归属验证 (double opt-in)。

挂载的 endpoint
- GET  /verify-email/<token>           → email_verify   （无需登录：链接本身是 auth）
- POST /users/<user_id>/resend-verify  → resend_verify  （登录 + 自助/admin）
"""
from __future__ import annotations

import logging
from typing import Any

from flask import Flask, flash, jsonify, redirect, render_template_string, request, url_for

from app.auth import self_or_admin_required, is_admin
from app.csrf import csrf_required
from app.db import storage
from users import get_user, load_users, update_users

logger = logging.getLogger(__name__)


_VERIFY_RESULT_HTML = """<!doctype html>
<html lang="{{ lang }}">
<head>
<meta charset="utf-8">
<title>{{ title }}</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, Inter, sans-serif;
         background:#0f1115; color:#e6e8eb;
         display:flex; align-items:center; justify-content:center;
         min-height:100vh; margin:0; padding:24px; }
  .card { max-width: 460px; padding:28px; border-radius:14px;
          background:#171a21; border:1px solid #242832;
          text-align:center; box-shadow:0 4px 30px rgba(0,0,0,.4); }
  .icon { font-size: 48px; margin-bottom:12px; }
  h1 { font-size:20px; margin: 0 0 8px; }
  p  { color:#9aa3ad; line-height:1.6; margin: 6px 0; }
  a  { color:#7c85e8; text-decoration:none; }
  .ok { color:#3eb877; }
  .err { color:#e26a6a; }
</style>
</head>
<body>
  <div class="card">
    <div class="icon">{{ icon }}</div>
    <h1 class="{{ status_class }}">{{ heading }}</h1>
    <p>{{ message }}</p>
    {% if email %}<p style="font-size:13px;color:#7c8593;">{{ email }}</p>{% endif %}
    <p style="margin-top:18px;"><a href="/">{{ home_link }}</a></p>
  </div>
</body>
</html>
"""


def _render_result(*, success: bool, email: str = "", lang: str = "zh") -> Any:
    """统一渲染验证结果页（成功/失败一致 UI 风格）。"""
    is_zh = lang == "zh"
    if success:
        return render_template_string(
            _VERIFY_RESULT_HTML,
            lang=lang,
            title="邮箱已确认" if is_zh else "Email confirmed",
            icon="✅",
            heading="邮箱已确认" if is_zh else "Email confirmed",
            message=("从现在起，FlatRadar 会把房源通知发到这个邮箱。"
                     if is_zh else
                     "FlatRadar will now send notifications to this address."),
            email=email,
            status_class="ok",
            home_link="返回首页" if is_zh else "Back to home",
        )
    return render_template_string(
        _VERIFY_RESULT_HTML,
        lang=lang,
        title="链接已失效" if is_zh else "Link expired",
        icon="⚠️",
        heading="链接已失效或无效" if is_zh else "Link expired or invalid",
        message=("这个验证链接可能已过期（24h）或已被消费过。"
                 "请到 FlatRadar 用户设置页重新触发验证邮件。"
                 if is_zh else
                 "This link may have expired (24h) or already been used. "
                 "Re-trigger verification from the FlatRadar user settings."),
        email="",
        status_class="err",
        home_link="返回首页" if is_zh else "Back to home",
    )


def email_verify(token: str) -> Any:
    """
    用户点击邮件里的验证链接 → 验证 token → 标记 user.email_verified=True。

    不需要登录：token 本身就是 auth 凭据。验证逻辑是 fail-closed：
    任何异常 / token 无效 / 过期 → 渲染失败页。
    """
    lang_cookie = request.cookies.get("h2s-lang", "zh")
    lang = lang_cookie if lang_cookie in ("zh", "en") else "zh"

    if not token or len(token) < 16 or len(token) > 128:
        return _render_result(success=False, lang=lang)

    st = storage()
    try:
        info = st.consume_email_verification(token)
    except Exception:
        logger.exception("email_verify: 校验 token 异常")
        st.close()
        return _render_result(success=False, lang=lang)
    finally:
        try: st.close()
        except Exception: pass

    if not info:
        return _render_result(success=False, lang=lang)

    user_id = info["user_id"]
    confirmed_email = info["email"]

    # 标记 user.email_verified=True，但仅当 user.email_to 仍等于该 email
    # （防御：用户在等待验证期间又把邮箱改成别的，旧 token 不应解锁新邮箱）
    try:
        def _mark(users):
            for u in users:
                if u.id == user_id and u.email_to == confirmed_email:
                    u.email_verified = True
                    return True
            return False

        updated = update_users(_mark)
    except Exception:
        logger.exception("email_verify: 写回 user.email_verified 异常")
        return _render_result(success=False, lang=lang)

    if not updated:
        # token 合法但当前 email_to 已经变了/用户被删了 → 不算成功
        return _render_result(success=False, lang=lang)

    logger.info(
        "邮箱验证通过 user_id=%s email=%s",
        user_id, _redact_email(confirmed_email),
    )
    return _render_result(success=True, email=confirmed_email, lang=lang)


def _redact_email(email: str) -> str:
    """简单脱敏 a***@x.com，仅供日志使用。"""
    if "@" not in email:
        return "***"
    local, _, domain = email.partition("@")
    if len(local) <= 1:
        return f"*@{domain}"
    return f"{local[0]}***@{domain}"


@self_or_admin_required
@csrf_required
def resend_verify(user_id: str) -> Any:
    """重新发送验证邮件。复用 send_verification_email_sync。"""
    from app.email_verify import EmailVerifyConfigError, send_verification_email_sync
    from app.auth import check_test_notify_rate, record_test_notify

    users = load_users()
    user = get_user(users, user_id)
    if user is None:
        return jsonify({"ok": False, "error": "用户不存在"}), 404
    if user.email_mode != "shared":
        return jsonify({"ok": False, "error": "仅共享模式需要验证"}), 400
    if not user.email_to:
        return jsonify({"ok": False, "error": "请先填写收件邮箱"}), 400
    if user.email_verified:
        return jsonify({"ok": True, "msg": "邮箱已验证，无需重发"})

    # 复用 test_notify 的限流通道（共享 quota，避免被用作免费邮件桥）
    if not is_admin():
        allowed, reason = check_test_notify_rate(user_id)
        if not allowed:
            return jsonify({"ok": False, "error": reason}), 429
        record_test_notify(user_id)

    try:
        sent = send_verification_email_sync(user.id, user.name, user.email_to)
    except EmailVerifyConfigError as e:
        logger.error("resend_verify: 邮箱验证未就绪: %s", e)
        return jsonify({"ok": False, "error": "系统未配置 PUBLIC_BASE_URL，暂时无法发送验证邮件"}), 503
    except Exception:
        logger.exception("resend_verify: 异常")
        return jsonify({"ok": False, "error": "发送失败"}), 500
    if not sent:
        return jsonify({"ok": False, "error": "服务器未配置邮件服务或临时故障"}), 503
    return jsonify({"ok": True, "msg": "验证邮件已重发"})


def register(app: Flask) -> None:
    app.add_url_rule(
        "/verify-email/<token>",
        endpoint="email_verify",
        view_func=email_verify,
        methods=["GET"],
    )
    app.add_url_rule(
        "/users/<user_id>/resend-verify",
        endpoint="resend_verify",
        view_func=resend_verify,
        methods=["POST"],
    )
