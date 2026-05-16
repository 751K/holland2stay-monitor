"""
鉴权 + 登录爆破防护 + 稳定 secret key
=======================================

提供
----
- auth_enabled / guest_mode_enabled / is_admin    : 状态查询
- login_required / api_login_required             : 普通会话装饰器
- admin_required / admin_api_required             : 管理员角色装饰器
- check_login_rate / record_login_failure /
  clear_login_failures                            : 爆破防护
- ensure_secret_key                               : 启动时获取或生成 FLASK_SECRET

依赖
----
- Flask request/session/jsonify/redirect/url_for
- config.ENV_PATH
- app.env_writer.write_env_key（带锁的 .env 写入）

设计要点
--------
本模块对 Flask 的依赖仅限于请求/会话级别（不持有 app 实例），
所以装饰器可以在任何 Blueprint / 路由上重复使用，与 endpoint 命名空间无关。
url_for("login") / url_for("index") 仍按 Stage 1 决定的扁平 endpoint 解析。
"""
from __future__ import annotations

import os
import secrets
import time as _time
from functools import wraps
from typing import Callable

from flask import jsonify, redirect, request, session, url_for

from config import ENV_PATH

from .env_writer import write_env_key

# ------------------------------------------------------------------ #
# 登录爆破防护：按 IP 记录连续失败次数，超阈值后施加指数退避延迟
# ------------------------------------------------------------------ #
_LOGIN_FAILURES: dict[str, list[float]] = {}
LOGIN_MAX_FAILURES = 5       # 连续失败 5 次后开始延迟
LOGIN_BASE_DELAY   = 1.0     # 首次延迟 1 秒，之后指数增长
_FAILURE_WINDOW    = 300     # 仅统计最近 5 分钟内的失败
_MAX_DELAY         = 30.0    # 单次最长 30 秒


def check_login_rate(ip: str) -> float:
    """返回当前 IP 应等待的秒数（0 表示无需等待）。"""
    now = _time.monotonic()
    window = [t for t in _LOGIN_FAILURES.get(ip, []) if now - t < _FAILURE_WINDOW]
    _LOGIN_FAILURES[ip] = window
    if len(window) < LOGIN_MAX_FAILURES:
        return 0.0
    extra = len(window) - LOGIN_MAX_FAILURES
    return min(LOGIN_BASE_DELAY * (2 ** extra), _MAX_DELAY)


def record_login_failure(ip: str) -> None:
    _LOGIN_FAILURES.setdefault(ip, []).append(_time.monotonic())


def clear_login_failures(ip: str) -> None:
    """登录成功后清除该 IP 的失败记录。"""
    _LOGIN_FAILURES.pop(ip, None)


# ------------------------------------------------------------------ #
# 注册滥用防护：同 IP 每小时最多 3 个新账号
# ------------------------------------------------------------------ #
_REGISTER_RECORDS: dict[str, list[float]] = {}
REGISTER_MAX_PER_HOUR = 3
_REGISTER_WINDOW = 3600  # 1 小时


def check_register_rate(ip: str) -> tuple[bool, str]:
    """
    返回 (allowed, reason)。
    同 IP 每小时内最多 REGISTER_MAX_PER_HOUR 个新账号。
    """
    now = _time.monotonic()
    records = [t for t in _REGISTER_RECORDS.get(ip, []) if now - t < _REGISTER_WINDOW]
    _REGISTER_RECORDS[ip] = records
    if len(records) >= REGISTER_MAX_PER_HOUR:
        oldest = min(records)
        remaining = int(_REGISTER_WINDOW - (now - oldest))
        minutes = max(1, remaining // 60)
        return False, f"注册过于频繁，请 {minutes} 分钟后再试"
    return True, ""


def record_registration(ip: str) -> None:
    """记录一次注册。"""
    _REGISTER_RECORDS.setdefault(ip, []).append(_time.monotonic())


# ------------------------------------------------------------------ #
# 状态查询
# ------------------------------------------------------------------ #

def auth_enabled() -> bool:
    """只有 WEB_PASSWORD 已设置才开启鉴权（向后兼容：未设置时无需登录）。"""
    return bool(os.environ.get("WEB_PASSWORD", "").strip())


def guest_mode_enabled() -> bool:
    """访客模式开关：默认开启，设 WEB_GUEST_MODE=false 可关闭。"""
    return os.environ.get("WEB_GUEST_MODE", "true").lower() != "false"


def is_admin() -> bool:
    """当前 session 是否为管理员（鉴权未开启时默认视为管理员）。"""
    if not auth_enabled():
        return True
    return session.get("role") == "admin"


# ------------------------------------------------------------------ #
# 装饰器
# ------------------------------------------------------------------ #

def login_required(f: Callable) -> Callable:
    """页面路由装饰器：未登录时跳转到登录页。"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if auth_enabled() and not session.get("authenticated"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated


def api_login_required(f: Callable) -> Callable:
    """API 路由装饰器：未登录时返回 401 JSON。"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if auth_enabled() and not session.get("authenticated"):
            return jsonify({"error": "unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


def admin_required(f: Callable) -> Callable:
    """页面路由装饰器：仅 admin 角色可访问；访客/游客重定向到首页。"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if auth_enabled():
            if not session.get("authenticated"):
                return redirect(url_for("login", next=request.path))
            if session.get("role") != "admin":
                return redirect(url_for("index"))
        return f(*args, **kwargs)
    return decorated


def admin_api_required(f: Callable) -> Callable:
    """API 路由装饰器：仅 admin 角色可访问，返回 401/403 JSON。"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if auth_enabled():
            if not session.get("authenticated"):
                return jsonify({"error": "unauthorized"}), 401
            if session.get("role") != "admin":
                return jsonify({"error": "forbidden"}), 403
        return f(*args, **kwargs)
    return decorated


# ------------------------------------------------------------------ #
# secret key 持久化
# ------------------------------------------------------------------ #

def ensure_secret_key() -> str:
    """
    稳定的 secret key：优先读 .env，不存在则自动生成并写入。

    保证重启后 session 不失效。写入失败时（容器只读 .env、磁盘满等）
    回退为本次进程内随机值——会话仍可用，只是重启后失效。
    """
    key = os.environ.get("FLASK_SECRET", "").strip()
    if key:
        return key
    key = secrets.token_hex(32)
    try:
        ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
        write_env_key("FLASK_SECRET", key)
        os.environ["FLASK_SECRET"] = key
    except Exception:
        pass
    return key
