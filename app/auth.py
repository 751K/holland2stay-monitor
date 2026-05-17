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
# 测试通知限流：防止用户把 /users/<id>/test 当成免费 mail relay 滥用
# ------------------------------------------------------------------ #
# 按 user_id 维度记录最近时间戳。两层窗口：
# - 每分钟 ≤ TEST_NOTIFY_PER_MINUTE 次
# - 每天   ≤ TEST_NOTIFY_PER_DAY    次（按滚动 24h 窗口，避开本地时区切换坑）
#
# admin 不限流（运维测试需求）。
_TEST_NOTIFY_TIMES: dict[str, list[float]] = {}
TEST_NOTIFY_PER_MINUTE = 3
TEST_NOTIFY_PER_DAY    = 20
_TEST_NOTIFY_MINUTE    = 60
_TEST_NOTIFY_DAY       = 86400


def check_test_notify_rate(user_id: str) -> tuple[bool, str]:
    """
    返回 (allowed, reason)。allowed=False 时 reason 是人类可读的拒绝理由。
    仅做 read-only 检查；命中限制不消耗配额。命中后调用方应直接拒绝；
    通过则继续调用 ``record_test_notify(user_id)`` 才正式占用一次。
    """
    if not user_id:
        return True, ""
    now = _time.monotonic()
    window = [t for t in _TEST_NOTIFY_TIMES.get(user_id, []) if now - t < _TEST_NOTIFY_DAY]
    _TEST_NOTIFY_TIMES[user_id] = window
    if len(window) >= TEST_NOTIFY_PER_DAY:
        return False, f"今日测试次数已达上限（{TEST_NOTIFY_PER_DAY}/天），请明天再试"
    in_minute = sum(1 for t in window if now - t < _TEST_NOTIFY_MINUTE)
    if in_minute >= TEST_NOTIFY_PER_MINUTE:
        return False, f"操作过于频繁，请稍后再试（{TEST_NOTIFY_PER_MINUTE} 次/分钟）"
    return True, ""


def record_test_notify(user_id: str) -> None:
    if not user_id:
        return
    _TEST_NOTIFY_TIMES.setdefault(user_id, []).append(_time.monotonic())


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


def is_user() -> bool:
    """当前 session 是否为普通登录用户（user 角色）。"""
    if not auth_enabled():
        return False
    return session.get("role") == "user"


def current_user_id() -> str:
    """返回 user 角色当前 session 绑定的 UserConfig.id，未登录或 admin/guest 返回空串。"""
    if not is_user():
        return ""
    return session.get("user_id", "") or ""


def _session_user_still_allowed() -> bool:
    """
    普通 user 的已登录 session 每次请求都重新确认账号仍可用。

    这样 admin 停用用户或关闭登录后，不需要等 cookie/session 过期，
    旧 Web 会话也会立即失效。admin / guest 不走 UserConfig 校验。
    """
    if not auth_enabled() or session.get("role") != "user":
        return True
    user_id = session.get("user_id", "") or ""
    if not user_id:
        return False
    try:
        from users import get_user, load_users
        user = get_user(load_users(), user_id)
    except Exception:
        return False
    return bool(user and user.enabled and user.app_login_enabled)


# ------------------------------------------------------------------ #
# 装饰器
# ------------------------------------------------------------------ #

def login_required(f: Callable) -> Callable:
    """页面路由装饰器：未登录时跳转到登录页。"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if auth_enabled() and not session.get("authenticated"):
            return redirect(url_for("login", next=request.path))
        if auth_enabled() and not _session_user_still_allowed():
            session.clear()
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated


def api_login_required(f: Callable) -> Callable:
    """API 路由装饰器：未登录时返回 401 JSON。"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if auth_enabled() and not session.get("authenticated"):
            return jsonify({"error": "unauthorized"}), 401
        if auth_enabled() and not _session_user_still_allowed():
            session.clear()
            return jsonify({"error": "用户已停用或登录已关闭"}), 401
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


def self_or_admin_required(f: Callable) -> Callable:
    """
    页面装饰器：允许 admin 访问任意 user_id，user 角色仅允许访问自己的 user_id。

    依赖：URL 必须含 ``<user_id>`` 路径参数（即 kwargs 中有 ``user_id``）。
    guest / 未登录 → 重定向登录页。
    user 角色访问别人的 ``user_id`` → 重定向首页（不暴露其他用户存在）。
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if not auth_enabled():
            return f(*args, **kwargs)
        if not session.get("authenticated"):
            return redirect(url_for("login", next=request.path))
        role = session.get("role")
        if role == "admin":
            return f(*args, **kwargs)
        if role == "user":
            if not _session_user_still_allowed():
                session.clear()
                return redirect(url_for("login", next=request.path))
            target = kwargs.get("user_id", "")
            if target and target == session.get("user_id"):
                return f(*args, **kwargs)
            return redirect(url_for("index"))
        # guest 或未知角色
        return redirect(url_for("index"))
    return decorated


def self_or_admin_api_required(f: Callable) -> Callable:
    """``self_or_admin_required`` 的 API 版本：未授权返回 401/403 JSON。"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not auth_enabled():
            return f(*args, **kwargs)
        if not session.get("authenticated"):
            return jsonify({"error": "unauthorized"}), 401
        role = session.get("role")
        if role == "admin":
            return f(*args, **kwargs)
        if role == "user":
            if not _session_user_still_allowed():
                session.clear()
                return jsonify({"error": "用户已停用或登录已关闭"}), 401
            target = kwargs.get("user_id", "")
            if target and target == session.get("user_id"):
                return f(*args, **kwargs)
            return jsonify({"error": "forbidden"}), 403
        return jsonify({"error": "forbidden"}), 403
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
