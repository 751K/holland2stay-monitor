"""
路由：会话相关（登录 / 登出 / 访客 / 语言切换）

挂载的 endpoint（保留扁平名，模板/前端零改动）
- GET/POST /login   → login
- POST    /logout   → logout
- POST    /guest    → guest_login
- GET     /set-lang → set_lang
"""
from __future__ import annotations

import hmac
import logging
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
    check_register_rate,
    check_login_rate,
    clear_login_failures,
    guest_mode_enabled,
    record_registration,
    record_login_failure,
)
from app.csrf import csrf_required
from app.safety import safe_next_url

logger = logging.getLogger(__name__)


def _render_login(*, next_value: str = "", status: int = 200):
    return (
        render_template(
            "login.html",
            next=next_value,
            auth_enabled=auth_enabled(),
            guest_mode=guest_mode_enabled(),
        ),
        status,
    )


def _try_user_login(username: str, password: str):
    """
    在 user_configs 表中尝试匹配 (username, password)。

    返回 (UserConfig | None, ok: bool)。
    - user 不存在 → (None, False)，但仍执行一次 bcrypt 计算抑制时序泄漏
    - 找到 user 但 app_login_enabled=False → (user, False)
    - app_login_enabled=True 且 bcrypt 通过 → (user, True)
    - 任何异常 → (None, False)，fail-closed

    复用 `verify_app_password()` 即可——它已经覆盖 enabled 检查 + 异常吞 + 空串拒。
    """
    try:
        from users import get_user_by_name, load_users, verify_app_password
        users = load_users()
        user = get_user_by_name(users, username)
        if user is None:
            # 没有该用户：仍消耗一次 bcrypt，避免可探测的时序差
            try:
                import bcrypt
                bcrypt.checkpw(b"x", bcrypt.hashpw(b"x", bcrypt.gensalt()))
            except Exception:
                pass
            return None, False
        ok = verify_app_password(user, password)
        return user, ok
    except Exception:
        return None, False


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
            session.pop("user_id", None)  # 旧 user 残留清掉，admin 不绑 user_id
            session["authenticated"] = True
            session["role"] = "admin"
            next_url = safe_next_url(request.form.get("next", ""))
            return redirect(next_url)

        # admin 失败 → 尝试 user 表登录（仅 app_login_enabled 且 bcrypt 通过）
        # 不论用户是否存在都走 bcrypt 校验路径以减少时序差异（fail-closed）。
        user_record, user_pass_ok = _try_user_login(username, password)
        if user_record is not None and user_pass_ok:
            clear_login_failures(client_ip)
            session.permanent = True
            session["authenticated"] = True
            session["role"] = "user"
            session["user_id"] = user_record.id
            next_url = safe_next_url(request.form.get("next", ""))
            return redirect(next_url)

        record_login_failure(client_ip)
        flash("用户名或密码错误", "danger")

    return _render_login(next_value=request.args.get("next", ""))


@csrf_required
def register_user() -> Any:
    """Web 注册：创建普通 user，设置 App/Web 登录密码，并直接登录。"""
    if not auth_enabled():
        return redirect(url_for("index"))
    if session.get("authenticated"):
        return redirect(url_for("index"))

    next_value = request.form.get("next", "")
    client_ip = request.remote_addr or "0.0.0.0"

    reg_ok, reg_reason = check_register_rate(client_ip)
    if not reg_ok:
        flash(reg_reason, "danger")
        return _render_login(next_value=next_value, status=429)

    username = request.form.get("register_username", "").strip()[:64]
    password = request.form.get("register_password", "")
    terms_accepted = request.form.get("terms_accepted") == "1"

    if not username or not password:
        flash("用户名和密码不能为空", "danger")
        return _render_login(next_value=next_value)
    if not terms_accepted:
        flash("请先确认使用条款与隐私政策", "danger")
        return _render_login(next_value=next_value, status=400)
    if len(username) < 2:
        flash("用户名至少需要 2 个字符", "danger")
        return _render_login(next_value=next_value)
    if len(password) < 4:
        flash("密码至少需要 4 个字符", "danger")
        return _render_login(next_value=next_value)
    if username.lower() == "__admin__" or username.startswith("__"):
        flash("该用户名不可用", "danger")
        return _render_login(next_value=next_value)

    try:
        from users import UserConfig, get_user_by_name, set_app_password, update_users

        def _append_user(users: list[UserConfig]) -> UserConfig:
            if get_user_by_name(users, username) is not None:
                raise ValueError("duplicate")
            user = UserConfig(
                name=username,
                enabled=True,
                notifications_enabled=False,
                app_login_enabled=True,
            )
            set_app_password(user, password)
            users.append(user)
            return user

        user = update_users(_append_user)
    except ValueError as e:
        if str(e) == "duplicate":
            flash("该用户名已被注册", "danger")
            return _render_login(next_value=next_value, status=409)
        raise
    except RuntimeError as e:
        logger.error("用户配置迁移/加载失败: %s", e)
        flash("用户配置加载失败，请联系管理员", "danger")
        return _render_login(next_value=next_value, status=500)
    except OSError:
        logger.exception("保存用户配置失败")
        flash("用户创建失败，请稍后再试", "danger")
        return _render_login(next_value=next_value, status=500)

    record_registration(client_ip)
    clear_login_failures(client_ip)
    logger.info("Web 新用户注册: name=%r id=%s ip=%s", username, user.id, client_ip)

    session.permanent = True
    session["authenticated"] = True
    session["role"] = "user"
    session["user_id"] = user.id
    return redirect(safe_next_url(next_value))


@csrf_required
def guest_login() -> Any:
    """访客模式：无需密码，直接以只读身份进入面板。需 POST + CSRF 防跨站攻击。"""
    if request.method != "POST":
        return redirect(url_for("login"))
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
    app.add_url_rule("/register", endpoint="register_user", view_func=register_user, methods=["POST"])
    app.add_url_rule("/logout",   endpoint="logout",      view_func=logout,      methods=["POST"])
    app.add_url_rule("/guest",    endpoint="guest_login", view_func=guest_login, methods=["POST"])
    app.add_url_rule("/set-lang", endpoint="set_lang",    view_func=set_lang,    methods=["GET"])
