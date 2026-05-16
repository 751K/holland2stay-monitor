"""
API v1 鉴权端点
================

- POST /api/v1/auth/login   : 用户名 + 密码 + 设备名 → 返回 Bearer token
- POST /api/v1/auth/logout  : 撤销当前 token
- GET  /api/v1/auth/me      : 返回当前身份（role + user 简要信息）

登录用户名命名约定
------------------
- "__admin__" → 用 WEB_PASSWORD 匹配，签发 role="admin" 的 token
- 其他        → 在 users.json 里按 name 找 UserConfig，校验：
                （1）优先 bcrypt app_password_hash
                （2）未设置或失败时回退到 H2S GraphQL customer token
                要求 app_login_enabled=True，签发 role="user" 的 token

guest 身份**不调** /auth/login——客户端本地标记，访问公开端点即可。
"""

from __future__ import annotations

import hmac
import json as _json
import logging
import os
import time as _time
from typing import Any

from flask import Blueprint, request

from app import api_auth, api_errors as _err
from app.auth import (
    check_register_rate,
    clear_login_failures,
    record_login_failure,
    record_registration,
)
from app.db import storage
from users import (
    UserConfig,
    get_user_by_name,
    load_users,
    save_users,
    set_app_password,
    verify_app_password,
)

logger = logging.getLogger(__name__)

ADMIN_USERNAME = "__admin__"

# 登录端点 TTL 边界：永远不允许客户端拿到非过期 token
DEFAULT_TTL_DAYS = 90
MAX_TTL_DAYS = 365
MIN_TTL_DAYS = 1

# 给"用户不存在"路径用的占位 bcrypt 哈希，用来抵消时序差
# 内容是 bcrypt("INVALID_DUMMY_PASSWORD_NEVER_MATCHES")，cost=12
# 任何用户输入与之 checkpw 都返回 False，但耗时与真实 bcrypt 一致。
_DUMMY_BCRYPT_HASH: str | None = None


def _dummy_bcrypt_verify(password: str) -> None:
    """
    跑一次 bcrypt.checkpw 但永远不会通过，用于"用户不存在"分支
    与真实用户分支的时序对齐。第一次调用时懒生成 hash 缓存到模块级。
    """
    global _DUMMY_BCRYPT_HASH
    try:
        import bcrypt
    except ImportError:
        return  # bcrypt 未安装，跳过时序对齐（不影响功能）
    if _DUMMY_BCRYPT_HASH is None:
        # 进程启动一次性开销，~100ms；之后只走 checkpw
        _DUMMY_BCRYPT_HASH = bcrypt.hashpw(
            b"__dummy_for_timing_alignment_only__", bcrypt.gensalt()
        ).decode("ascii")
    try:
        bcrypt.checkpw(password.encode("utf-8"), _DUMMY_BCRYPT_HASH.encode("ascii"))
    except Exception:
        pass  # 任何异常都吞掉，这只是用于时序对齐


# ── H2S 凭据验证 ──────────────────────────────────────────────────────

_GQL_LOGIN_MUTATION = """
mutation($email: String!, $password: String!) {
  generateCustomerToken(email: $email, password: $password) {
    token
  }
}
"""

_GQL_HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://www.holland2stay.com",
    "Referer": "https://www.holland2stay.com/",
    "Accept": "application/json",
}


def verify_h2s_credentials(email: str, password: str) -> bool:
    """调用 H2S GraphQL generateCustomerToken 验证凭据。"""
    import curl_cffi.requests as _req
    from config import get_impersonate as _get_impersonate

    try:
        with _req.Session(impersonate=_get_impersonate()) as session:
            resp = session.post(
                "https://api.holland2stay.com/graphql/",
                json={
                    "query": _GQL_LOGIN_MUTATION,
                    "variables": {"email": email, "password": password},
                },
                headers=_GQL_HEADERS,
                timeout=15,
            )
    except Exception:
        logger.exception("H2S 凭据验证网络异常")
        return False

    if resp.status_code != 200:
        logger.info("H2S 凭据验证 HTTP %d", resp.status_code)
        return False

    try:
        body = resp.json()
        token = (
            (body.get("data") or {})
            .get("generateCustomerToken") or {}
        ).get("token")
        return bool(token)
    except Exception:
        logger.exception("H2S 凭据验证 JSON 解析失败")
        return False


def _login() -> Any:
    """POST /auth/login —— JSON body: {username, password, device_name?, ttl_days?}"""
    # 限流：复用 Web 后台的 IP 退避，行为完全一致
    allowed, wait = api_auth.login_rate_check()
    if not allowed:
        # 与 sessions.py 一致：先 sleep 再返回，制造延迟成本
        _time.sleep(wait)

    body = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    device_name = (body.get("device_name") or "").strip()[:64] or "未命名设备"

    # TTL 解析（fail-closed）：客户端永远不能拿到永不过期 token。
    # 任何 None / 缺失 / 非法值 / 越界 → 退回默认 90 天。
    ttl_days_raw = body.get("ttl_days", DEFAULT_TTL_DAYS)
    try:
        ttl_days = int(ttl_days_raw) if ttl_days_raw is not None else DEFAULT_TTL_DAYS
    except (TypeError, ValueError):
        ttl_days = DEFAULT_TTL_DAYS
    if ttl_days < MIN_TTL_DAYS or ttl_days > MAX_TTL_DAYS:
        ttl_days = DEFAULT_TTL_DAYS

    if not username or not password:
        return _err.err_validation("缺少 username 或 password")

    client_ip = request.remote_addr or "?"

    role: str | None = None
    user_id: str | None = None

    if username == ADMIN_USERNAME:
        expected = os.environ.get("WEB_PASSWORD", "")
        # 没设密码 = Web 端鉴权未开启 = App 也不允许登录（fail-closed）
        if not expected:
            record_login_failure(client_ip)
            return _err.err_unauthorized("管理员密码未配置，无法登录")
        # 时序常数比较
        if hmac.compare_digest(password.encode("utf-8"), expected.encode("utf-8")):
            role = "admin"
            user_id = None
        else:
            record_login_failure(client_ip)
            return _err.err_unauthorized("用户名或密码错误")
    else:
        try:
            users = load_users()
        except RuntimeError as e:
            logger.error("users.json 解析失败: %s", e)
            return _err.err_server_error(e, "用户配置文件损坏，请联系管理员")
        user = get_user_by_name(users, username)
        # 时序对齐：无论用户存在与否，都跑一次 bcrypt（真或 dummy），
        # 否则 ~100ms 的时序差让攻击者能枚举出真实用户名。
        if user is None:
            _dummy_bcrypt_verify(password)
            record_login_failure(client_ip)
            return _err.err_unauthorized("用户名或密码错误")
        if not user.enabled:
            _dummy_bcrypt_verify(password)
            record_login_failure(client_ip)
            return _err.err_forbidden("该用户已停用")
        authed = verify_app_password(user, password)
        if not authed:
            # 回退到 H2S 凭据验证（允许用户直接用 H2S 账号密码登录）
            logger.info("app_password 校验失败或未设置，尝试 H2S 凭据 user=%s", user.name)
            authed = verify_h2s_credentials(username, password)
        if not authed:
            record_login_failure(client_ip)
            return _err.err_unauthorized("用户名或密码错误")
        role = "user"
        user_id = user.id

    # 通过：签发 token
    clear_login_failures(client_ip)
    st = storage()
    try:
        token_id, plaintext = st.create_app_token(
            role=role,
            user_id=user_id,
            device_name=device_name,
            ttl_days=ttl_days,
        )
    finally:
        st.close()

    logger.info(
        "API 登录: role=%s user_id=%s device=%r ip=%s token_id=%d",
        role, user_id, device_name, client_ip, token_id,
    )

    return _err.ok({
        "token": plaintext,
        "token_id": token_id,
        "role": role,
        "user_id": user_id,
        "device_name": device_name,
        "ttl_days": ttl_days,
    })


def _logout() -> Any:
    """POST /auth/logout —— 撤销当前 token。"""
    token_id = api_auth.current_token_id()
    if token_id is None:
        return _err.err_unauthorized()

    st = storage()
    try:
        changed = st.revoke_app_token(token_id)
    finally:
        st.close()

    # 立即从 TTL 缓存中清掉，避免 5 分钟内仍然能用
    api_auth.invalidate_token_cache()
    return _err.ok({"revoked": bool(changed)})


def _me() -> Any:
    """GET /auth/me —— 当前身份摘要。"""
    role = api_auth.current_role()
    user_id = api_auth.current_user_id()

    payload: dict = {"role": role, "user_id": user_id, "user": None}

    if role == "user" and user_id:
        try:
            users = load_users()
        except RuntimeError:
            return _err.err_server_error(None, "用户配置文件损坏")
        u = next((x for x in users if x.id == user_id), None)
        if u is None:
            return _err.err_unauthorized("用户已被删除")
        # 只返回 App 端用得到的字段；不返回敏感凭证（imap/smtp/twilio）
        from dataclasses import asdict
        lf = asdict(u.listing_filter)
        payload["user"] = {
            "id": u.id,
            "name": u.name,
            "enabled": u.enabled,
            "notifications_enabled": u.notifications_enabled,
            "listing_filter": lf,
        }
    return _err.ok(payload)


# ── 注册 ─────────────────────────────────────────────────────────────

def _register() -> Any:
    """
    POST /auth/register —— JSON body: {username, password, device_name?, ttl_days?}

    创建新用户到 users.json，自动设置 app 登录密码（bcrypt），
    并同时签发 token 实现"注册即登录"。

    安全边界
    --------
    - 用户名不能是 "__admin__"（保留给管理员）
    - 用户名不能与已有用户重复
    - 密码最少 4 字符
    - 复用 login_rate_check 防止批量注册
    - 新用户 notifications_enabled=False（fail-closed，等用户在 Web 面板
      配置好通知渠道后再开启）
    """
    client_ip = request.remote_addr or "?"

    allowed, wait = api_auth.login_rate_check()
    if not allowed:
        _time.sleep(wait)

    # 注册专用限流：同 IP 每小时最多 3 个
    reg_ok, reg_reason = check_register_rate(client_ip)
    if not reg_ok:
        return _err.err_rate_limited(reg_reason)

    body = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    device_name = (body.get("device_name") or "").strip()[:64] or "未命名设备"

    ttl_days_raw = body.get("ttl_days", DEFAULT_TTL_DAYS)
    try:
        ttl_days = int(ttl_days_raw) if ttl_days_raw is not None else DEFAULT_TTL_DAYS
    except (TypeError, ValueError):
        ttl_days = DEFAULT_TTL_DAYS
    if ttl_days < MIN_TTL_DAYS or ttl_days > MAX_TTL_DAYS:
        ttl_days = DEFAULT_TTL_DAYS

    # 验证
    if not username or not password:
        return _err.err_validation("用户名和密码不能为空")
    if len(username) < 2:
        return _err.err_validation("用户名至少需要 2 个字符")
    if len(password) < 4:
        return _err.err_validation("密码至少需要 4 个字符")
    if username.lower() == ADMIN_USERNAME:
        return _err.err_validation("该用户名不可用")

    try:
        users = load_users()
    except RuntimeError as e:
        logger.error("users.json 解析失败: %s", e)
        return _err.err_server_error(e, "用户配置文件损坏，请联系管理员")

    if get_user_by_name(users, username) is not None:
        return _err.err_conflict("该用户名已被注册")

    # 创建用户
    user = UserConfig(
        name=username,
        enabled=True,
        notifications_enabled=False,
        app_login_enabled=True,
    )
    set_app_password(user, password)
    users.append(user)

    try:
        save_users(users)
    except OSError as e:
        logger.exception("保存 users.json 失败")
        return _err.err_server_error(e, "用户创建失败，请稍后再试")

    record_registration(client_ip)
    logger.info("新用户注册: name=%r id=%s ip=%s", username, user.id, client_ip)

    # 签发 token（注册即登录）
    clear_login_failures(request.remote_addr or "?")
    st = storage()
    try:
        token_id, plaintext = st.create_app_token(
            role="user",
            user_id=user.id,
            device_name=device_name,
            ttl_days=ttl_days,
        )
    finally:
        st.close()

    return _err.ok({
        "token": plaintext,
        "token_id": token_id,
        "role": "user",
        "user": {
            "id": user.id,
            "name": user.name,
            "enabled": user.enabled,
            "notifications_enabled": user.notifications_enabled,
            "listing_filter": {},
        },
    }, status=201)


# ── Blueprint 注册 ───────────────────────────────────────────────────

def register(bp: Blueprint) -> None:
    bp.add_url_rule(
        "/auth/login",
        endpoint="auth_login",
        view_func=_login,
        methods=["POST"],
    )
    bp.add_url_rule(
        "/auth/register",
        endpoint="auth_register",
        view_func=_register,
        methods=["POST"],
    )
    bp.add_url_rule(
        "/auth/logout",
        endpoint="auth_logout",
        view_func=api_auth.bearer_required(("admin", "user"))(_logout),
        methods=["POST"],
    )
    bp.add_url_rule(
        "/auth/me",
        endpoint="auth_me",
        view_func=api_auth.bearer_required(("admin", "user"))(_me),
        methods=["GET"],
    )
