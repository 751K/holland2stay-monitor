"""
CSRF 防护：纵深防御，配合 SameSite=Lax 双重保障
====================================================

策略
----
- token 绑定到当前 session（fixed-token 模式），随 session cookie 一起管理
- POST 请求强制校验：表单字段 csrf_token 或请求头 X-CSRF-Token
- 校验使用 hmac.compare_digest 防时序攻击
- 校验失败返回 403，不泄露具体原因

依赖
----
- Flask request/session/abort
"""
from __future__ import annotations

import hmac
import secrets
from functools import wraps
from typing import TYPE_CHECKING, Callable

from flask import abort, request, session

if TYPE_CHECKING:
    from flask import Flask


def get_csrf_token() -> str:
    """
    获取（或首次生成）绑定到当前 session 的 CSRF token。

    每个 session 生成一次，不随请求更换；fixed-token 模式足以防御 CSRF。
    """
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)
    return session["csrf_token"]


def csrf_required(f: Callable) -> Callable:
    """
    路由装饰器：对 POST 请求验证 CSRF token。

    token 来源（任一即可）：
    - 表单字段  : csrf_token
    - 请求头    : X-CSRF-Token（fetch / XHR 调用使用）
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.method == "POST":
            token = (request.form.get("csrf_token")
                     or request.headers.get("X-CSRF-Token", ""))
            expected = session.get("csrf_token", "")
            if not token or not expected or not hmac.compare_digest(token, expected):
                abort(403)
        return f(*args, **kwargs)
    return decorated


def register(app: "Flask") -> None:
    """把 csrf_token() 注册为 Jinja 全局函数；模板里直接调用 csrf_token()。"""
    app.jinja_env.globals["csrf_token"] = get_csrf_token
