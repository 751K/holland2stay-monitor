"""
API v1 Bearer Token 鉴权
==========================

提供
----
- bearer_required(allow_roles)  : 严格 Bearer 校验，role 不匹配返回 403
- bearer_optional               : 可选 Bearer（用于"既支持登录又支持游客"的端点）
- current_role / current_user_id / current_token_id : 取当前请求的身份

调用约定
--------
所有 ``/api/v1/*`` 路由用 ``@bearer_required(...)`` 或 ``@bearer_optional``。
未带 token 且端点未 ``bearer_optional`` 时一律 401，与 Web 端 cookie session
完全解耦——后者继续走 ``app/auth.py``。

性能与一致性
------------
1. **TTL 缓存**：每个 token 验证只在 5 分钟内查一次 SQLite；超时或撤销时
   失效。500 token / 5 min 内开销约一次 sha256 + dict 查询。
2. **last_used_at 异步刷新**：每请求只往一个内存 set 投递 token_id，
   后台守护线程每 30s flush 一次。Flask debug reload 时由 atexit 收尾。
3. **失败永远 fail-closed**：bcrypt/DB 异常都视为 401，不暴露内部错误。

线程安全
--------
- _CACHE 用普通 dict + threading.Lock（写少读多，竞争极低）
- _PENDING_TOUCH 是 set；append/clear 都在 lock 内
- _flush 在自己的守护线程，与 Gunicorn 多线程 worker 互不阻塞
"""

from __future__ import annotations

import logging
import threading
import time
from functools import wraps
from typing import Callable, Optional

from flask import g, request

from . import api_errors as _err
from .auth import check_login_rate

logger = logging.getLogger(__name__)


# ── 模块状态 ────────────────────────────────────────────────────────

# token_hash -> (row_dict, cached_at_monotonic)
_CACHE: dict[str, tuple[dict, float]] = {}
_CACHE_TTL = 300.0                           # 5 分钟
_CACHE_MAX = 1024                            # 上限，防极端情况内存涨

# 待刷 last_used_at 的 token_id 集合
_PENDING_TOUCH: set[int] = set()
_FLUSH_INTERVAL = 30.0                       # 秒

_lock = threading.Lock()
_flush_thread: Optional[threading.Thread] = None
_flush_stop = threading.Event()


# ── Cache 操作 ─────────────────────────────────────────────────────

def _cache_get(token_hash: str) -> Optional[dict]:
    now = time.monotonic()
    with _lock:
        entry = _CACHE.get(token_hash)
        if not entry:
            return None
        row, cached_at = entry
        if now - cached_at > _CACHE_TTL:
            _CACHE.pop(token_hash, None)
            return None
        return row


def _cache_put(token_hash: str, row: dict) -> None:
    now = time.monotonic()
    with _lock:
        if len(_CACHE) >= _CACHE_MAX:
            # 简单粗暴：超限时丢一半最旧的（按 cached_at）
            # 这种情况只在攻击/异常时发生，不需要精确 LRU
            oldest = sorted(_CACHE.items(), key=lambda kv: kv[1][1])
            for k, _ in oldest[: _CACHE_MAX // 2]:
                _CACHE.pop(k, None)
        _CACHE[token_hash] = (row, now)


def invalidate_token_cache(token_hash: Optional[str] = None) -> None:
    """撤销 token 时调用；不传参清空全部缓存。"""
    with _lock:
        if token_hash is None:
            _CACHE.clear()
        else:
            _CACHE.pop(token_hash, None)


# ── last_used_at 批量刷盘 ──────────────────────────────────────────

def _schedule_touch(token_id: int) -> None:
    with _lock:
        _PENDING_TOUCH.add(token_id)


def _flush_pending() -> None:
    """从 _PENDING_TOUCH 取出全部 id，批量写库。本身吞所有异常。"""
    with _lock:
        if not _PENDING_TOUCH:
            return
        ids = list(_PENDING_TOUCH)
        _PENDING_TOUCH.clear()
    try:
        from app.db import storage
        st = storage()
        try:
            st.touch_app_tokens(ids)
        finally:
            st.close()
    except Exception:
        logger.exception("flush last_used_at failed")


def _flush_loop() -> None:
    while not _flush_stop.wait(_FLUSH_INTERVAL):
        _flush_pending()
    # 退出前最后一次 flush
    _flush_pending()


def _ensure_flush_thread() -> None:
    global _flush_thread
    if _flush_thread is not None and _flush_thread.is_alive():
        return
    with _lock:
        if _flush_thread is not None and _flush_thread.is_alive():
            return
        t = threading.Thread(
            target=_flush_loop,
            name="api-auth-flush",
            daemon=True,
        )
        t.start()
        _flush_thread = t


def stop_flush_thread() -> None:
    """测试/优雅关停用——通常不需要主动调。"""
    _flush_stop.set()
    if _flush_thread is not None:
        _flush_thread.join(timeout=5)


# ── 当前请求的身份取值 ─────────────────────────────────────────────

def current_role() -> Optional[str]:
    """'admin' / 'user' / None（未鉴权或 bearer_optional 未带 token）。"""
    return getattr(g, "api_role", None)


def current_user_id() -> Optional[str]:
    """role='user' 时返回 UserConfig.id；admin/guest 返回 None。"""
    return getattr(g, "api_user_id", None)


def current_token_id() -> Optional[int]:
    """当前 token 在 app_tokens 表中的主键，None 表示未鉴权。"""
    return getattr(g, "api_token_id", None)


# ── 核心解析 ────────────────────────────────────────────────────────

def _extract_bearer() -> Optional[str]:
    h = request.headers.get("Authorization", "")
    if not h.lower().startswith("bearer "):
        return None
    tok = h[7:].strip()
    return tok or None


def _resolve_token(plaintext: str) -> Optional[dict]:
    """
    plaintext -> row dict，None 表示验证失败。

    成功路径：
    1. 计算 hash
    2. 查 cache；命中且未过期/未撤销 → 返回
    3. 查 DB；命中且通过校验 → 入 cache → 返回
    任何一步失败/抛错都返回 None（fail-closed）。
    """
    from mstorage._tokens import hash_token

    token_hash = hash_token(plaintext)

    cached = _cache_get(token_hash)
    if cached is not None:
        return cached

    try:
        from app.db import storage
        st = storage()
        try:
            row = st.find_app_token(token_hash)
        finally:
            st.close()
    except Exception:
        logger.exception("find_app_token failed")
        return None

    if not row:
        return None
    if row.get("revoked"):
        return None
    expires_at = row.get("expires_at")
    if expires_at:
        # ISO8601 UTC 字符串可以直接字典序比较（同一格式同一时区）
        import datetime as _dt
        now_iso = _dt.datetime.now(_dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        if expires_at < now_iso:
            return None

    _cache_put(token_hash, row)
    return row


# ── 装饰器 ──────────────────────────────────────────────────────────

def bearer_required(allow_roles: tuple[str, ...] = ("admin", "user")) -> Callable:
    """
    严格 Bearer：必须带合法 token，role 不在白名单返回 403。

    用法：
        @bp.get("/things")
        @bearer_required(allow_roles=("user",))
        def list_things(): ...

    成功时设置：
        g.api_role     = "admin" | "user"
        g.api_user_id  = UserConfig.id | None
        g.api_token_id = int
    """
    _ensure_flush_thread()

    def deco(f: Callable) -> Callable:
        @wraps(f)
        def w(*args, **kwargs):
            tok = _extract_bearer()
            if not tok:
                return _err.err_unauthorized()
            row = _resolve_token(tok)
            if not row:
                return _err.err_unauthorized("token 无效或已过期")
            if row["role"] not in allow_roles:
                return _err.err_forbidden()
            g.api_role = row["role"]
            g.api_user_id = row.get("user_id")
            g.api_token_id = row["id"]
            _schedule_touch(row["id"])
            return f(*args, **kwargs)
        return w
    return deco


def bearer_optional(f: Callable) -> Callable:
    """
    可选 Bearer：未带 token 时 role=None（guest）；带 token 走 strict 校验。

    用法：
        @bp.get("/stats/public/summary")
        @bearer_optional
        def summary(): ...
    """
    _ensure_flush_thread()

    @wraps(f)
    def w(*args, **kwargs):
        tok = _extract_bearer()
        if tok:
            row = _resolve_token(tok)
            if row:
                g.api_role = row["role"]
                g.api_user_id = row.get("user_id")
                g.api_token_id = row["id"]
                _schedule_touch(row["id"])
            # 带了 token 但无效 → 仍然继续以 guest 身份；
            # 这样 401 不会卡住 "随便看看" 的 guest 端点。
        return f(*args, **kwargs)
    return w


# ── 登录限流 ─────────────────────────────────────────────────────────

def login_rate_check() -> tuple[bool, float]:
    """
    供 /api/v1/auth/login 复用 Web 后台的 IP 退避。

    返回 (allowed, wait_seconds)：
    - allowed=True  : 直接放行
    - allowed=False : 应当响应 429，message 提示等待 wait_seconds 秒
    """
    ip = request.remote_addr or "?"
    wait = check_login_rate(ip)
    return (wait == 0.0, wait)
