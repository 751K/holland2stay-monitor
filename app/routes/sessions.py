"""
路由：会话相关（登录 / 登出 / 访客 / 语言切换）

挂载的 endpoint（保留扁平名，模板/前端零改动）
- GET/POST /login   → login
- POST    /logout   → logout
- GET     /guest    → guest_login
- GET     /set-lang → set_lang
"""
from __future__ import annotations

import hmac
import os
import time as _time
from typing import Any

from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from app.auth import (
    auth_enabled,
    check_login_rate,
    clear_login_failures,
    guest_mode_enabled,
    record_login_failure,
)
from app.csrf import csrf_required
from app.safety import safe_next_url


@csrf_required
def login() -> Any:
    # 如果鉴权未启用，直接跳首页
    if not auth_enabled():
        return redirect(url_for("index"))
    # 已登录也跳首页
    if session.get("authenticated"):
        return redirect(url_for("index"))

    if request.method == "POST":
        # 爆破防护：连续失败超阈值后指数退避
        client_ip = request.remote_addr or "0.0.0.0"
        delay = check_login_rate(client_ip)
        if delay > 0:
            _time.sleep(delay)

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        # WEB_USERNAME 未设置时默认为 "admin"
        expected_user = os.environ.get("WEB_USERNAME", "").strip() or "admin"
        expected_pass = os.environ.get("WEB_PASSWORD", "")

        # 用 hmac.compare_digest 防时序攻击。
        # 必须先 .encode("utf-8")：hmac.compare_digest 对含非 ASCII 字符的
        # str 参数会抛 TypeError —— 攻击者用中文/emoji 用户名能直接让 /login
        # 返回 500。改用 bytes 形式，任意 unicode 都安全比较，且时序常数保留。
        user_ok = hmac.compare_digest(username.encode("utf-8"), expected_user.encode("utf-8"))
        pass_ok = hmac.compare_digest(password.encode("utf-8"), expected_pass.encode("utf-8"))
        if user_ok and pass_ok:
            clear_login_failures(client_ip)  # 成功则清除失败记录
            session.permanent = True
            session["authenticated"] = True
            session["role"] = "admin"
            next_url = safe_next_url(request.form.get("next", ""))
            return redirect(next_url)

        record_login_failure(client_ip)
        flash("用户名或密码错误", "danger")

    return render_template(
        "login.html",
        next=request.args.get("next", ""),
        auth_enabled=auth_enabled(),
        guest_mode=guest_mode_enabled(),
    )


def guest_login() -> Any:
    """访客模式：无需密码，直接以只读身份进入面板。"""
    if not auth_enabled():
        return redirect(url_for("index"))
    if not guest_mode_enabled():
        return redirect(url_for("login"))
    # 已登录的 admin 不允许被降级为 guest（防止误操作或 CSRF 降级攻击）
    if session.get("role") == "admin":
        return redirect(url_for("index"))
    session.permanent = True
    session["authenticated"] = True
    session["role"] = "guest"
    return redirect(url_for("index"))


@csrf_required
def logout() -> Any:
    session.clear()
    return redirect(url_for("login"))


def set_lang() -> Any:
    lang = request.args.get("lang", "zh")
    if lang not in ("zh", "en"):
        lang = "zh"
    resp = redirect(safe_next_url(request.args.get("next", "")))
    resp.set_cookie("h2s-lang", lang, max_age=60 * 60 * 24 * 365, samesite="Lax")
    return resp


def register(app: Flask) -> None:
    app.add_url_rule("/login",    endpoint="login",       view_func=login,       methods=["GET", "POST"])
    app.add_url_rule("/logout",   endpoint="logout",      view_func=logout,      methods=["POST"])
    app.add_url_rule("/guest",    endpoint="guest_login", view_func=guest_login, methods=["GET"])
    app.add_url_rule("/set-lang", endpoint="set_lang",    view_func=set_lang,    methods=["GET"])
