"""
Holland2Stay 监控 Web 面板 — 应用引导层
==========================================

职责
----
本文件**只**负责 Flask app 的引导与组装：
- 实例化 Flask，配置 session cookie / 安全头
- 注册 CSRF + Jinja 过滤器
- 注册全局 context_processor（i18n + 鉴权状态）
- 依次调用各 ``app.routes.*`` 模块的 ``register(app)``

所有具体路由实现已拆分到 ``app/routes/`` 下的独立模块。
所有共享工具已拆分到 ``app/`` 的对应模块（auth / csrf / i18n / ...）。

运行方式
--------
    python web.py               # 本地开发，默认 http://localhost:8088
    python web.py --port 8080   # 自定义端口

Docker 容器中由 Gunicorn 启动（supervisord.conf）：
    gunicorn --workers=1 --threads=8 --timeout=0 --bind=0.0.0.0:8088 web:app
    （直接运行 python web.py 仅用于本地调试）
"""
from __future__ import annotations

import argparse
import hashlib
import logging.handlers
import os
import sys
import time
from pathlib import Path

from flask import Flask, g, request

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import ASSETS_DIR, DATA_DIR  # noqa: E402
from translations import tr as _tr  # noqa: E402

# 配置 Web 进程日志：独立文件 data/web.log，避免与 monitor 进程写冲突。
# 注意：此文件记录 Flask 应用自身的日志（请求处理、配置变更等），
# 与 supervisord 重定向的 Gunicorn stdout（/app/logs/web.log）是不同文件。
# Web 面板「日志查看」页面读取的是本文件。
_DATA_DIR = Path(os.environ.get("DATA_DIR", str(DATA_DIR)))
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_fh = logging.handlers.RotatingFileHandler(
    str(_DATA_DIR / "web.log"),
    maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8",
)
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
_fh.setLevel(logging.INFO)
logging.getLogger().setLevel(logging.INFO)
logging.getLogger().addHandler(_fh)
# 屏蔽 Werkzeug HTTP 访问日志，只保留 WARNING+（如 5xx 错误）
logging.getLogger("werkzeug").setLevel(logging.WARNING)

# app/ 子包
from app import csrf as _csrf                                    # noqa: E402
from app import early_hints                                      # noqa: E402
from app import jinja_filters                                    # noqa: E402
from app.auth import (                                            # noqa: E402
    auth_enabled,
    current_user_id,
    ensure_secret_key,
    guest_mode_enabled,
    is_admin,
    is_user,
)
from app.i18n import get_lang                                     # noqa: E402
from app.routes import (                                          # noqa: E402
    app_accounts,
    calendar_routes,
    control,
    dashboard,
    email_verify as email_verify_routes,
    inbound,
    legal,
    map_routes,
    notifications,
    sessions,
    settings as settings_routes,
    site_meta,
    stats,
    system,
    users,
)
from app.routes import api_v1                                    # noqa: E402

# ------------------------------------------------------------------ #
# 运维配置注水
# ------------------------------------------------------------------ #
#
# runtime 类配置住在 app_settings 表里，得在读任何配置之前注入 os.environ。
# 放在模块级而不是 main()：gunicorn 加载的是 web:app，根本不经过 main()。
# 每个 worker 各注一次，这是必要的——它们是独立进程，各有自己的 os.environ。
#
# 只注水不迁移。迁移要改写 .env，多个 worker 并发改同一个文件会打架；那一步交给
# monitor 独占（见 monitor._bootstrap_settings）。两个进程读同一个库，monitor
# 搬完这边下次注水就看得到。
def _hydrate_settings() -> None:
    try:
        from config import DB_PATH, TIMEZONE
        from settings_store import hydrate
        from storage import Storage

        st = Storage(DB_PATH, timezone_str=TIMEZONE)
        try:
            hydrate(st)
        finally:
            st.close()
    except Exception:
        logging.getLogger(__name__).warning(
            "载入 app_settings 失败，本 worker 使用 .env / 默认值", exc_info=True,
        )


_hydrate_settings()

# ------------------------------------------------------------------ #
# Flask app
# ------------------------------------------------------------------ #

app = Flask(
    __name__,
    template_folder=str(ASSETS_DIR / "templates"),
    static_folder=str(ASSETS_DIR / "static"),
)

# SameSite=Lax：阻止跨站 POST 请求携带 session cookie（主要 CSRF 防护层）。
# HttpOnly=True：禁止 JS 读取 session cookie（Flask 默认已是 True，此处显式声明）。
# Secure=True：仅 HTTPS 下发送 cookie；本地开发通过 SESSION_COOKIE_SECURE=false 关闭。
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SECURE"]   = os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true"
app.config["PERMANENT_SESSION_LIFETIME"] = int(os.environ.get("SESSION_LIFETIME_HOURS", "24")) * 3600

# 稳定的 secret key：优先读 .env，不存在则自动生成并写入
app.secret_key = ensure_secret_key()


@app.teardown_appcontext
def _close_request_storage(exc=None):
    """关闭请求作用域的 SQLite 连接（若存在）。

    仅关闭由 app.db.storage() 标记为 _teardown_managed=True 的实例；
    路由内部自行创建的 Storage（如 SSE 生成器内的）不受影响。
    """
    st = g.pop('_storage', None)
    if st is not None:
        # 绕过 _teardown_managed 检查，强制关闭
        st._teardown_managed = False
        st.close()


@app.after_request
def _add_security_headers(resp):
    resp.headers.setdefault("X-Frame-Options",        "DENY")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy",        "strict-origin-when-cross-origin")
    resp.headers.setdefault("Strict-Transport-Security",
                            "max-age=63072000; includeSubDomains; preload")
    # CSP: allow self, inline styles (design.css vars), Google Fonts, CDN (icons + charts), maps.
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net https://unpkg.com; "
        "font-src 'self' https://fonts.gstatic.com https://cdn.jsdelivr.net; "
        "img-src 'self' data: https://*.tile.openstreetmap.org https://maps.googleapis.com https://maps.gstatic.com; "
        "script-src 'self' 'unsafe-inline' https://unpkg.com https://cdn.jsdelivr.net https://maps.googleapis.com; "
        "connect-src 'self' https://maps.googleapis.com https://maps.gstatic.com; "
        "worker-src 'self' blob:; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'",
    )

    # ── 语言协商要对缓存可见 ──────────────────────────────────────
    # 同一个 URL 会按 Accept-Language 返回中文或英文（app.i18n.get_lang）。
    # 不声明 Vary 的话，任何中间缓存（Cloudflare、公司代理、浏览器）都会把
    # 第一个访客拿到的那份语言发给后面所有人。
    #
    # 只给 HTML 加：/static/ 下的资源与语言无关，给它们加 Vary 只会白白降低
    # 边缘缓存命中率。
    #
    # 用「读出来再合并」而不是直接赋值，是为了不覆盖**视图自己或更早跑的
    # after_request 已经写好的** Vary（本文件是目前唯一的 after_request，但
    # Flask 按注册的逆序执行，后加的会跑在前面）。
    # 注意 session 的 `Vary: Cookie` **不在此列**：它由 Flask 的 process_response
    # 在所有 after_request 之后追加，这里根本看不到它——最终响应上两个都在，
    # 靠的是 Flask 那一步，不是这里的合并。
    if resp.mimetype == "text/html":
        parts = [p.strip() for p in resp.headers.get("Vary", "").split(",") if p.strip()]
        if not any(p.lower() == "accept-language" for p in parts):
            resp.headers["Vary"] = ", ".join(parts + ["Accept-Language"])

    # ── 静态资源缓存 ──────────────────────────────────────────────
    # /static/ 下的文件都通过 `?v=NN` 查询字符串做 cache bust（design.css?v=15、
    # app.js?v=5 等）。URL 不变就能命中缓存。
    #
    # 缓存周期 30 天：在"重复访问命中率高"和"忘记 bump v=N 时痛苦时长"
    # 之间折中——理论可以拉到 1 年（immutable 标准做法），但如果某次部署
    # 改了 design.css 忘记 bump，用户/Cloudflare 边缘缓存会卡 1 年；30
    # 天意味着最差 30 天内就会自动 revalidate。
    #
    # 不带版本号的 favicon / logo 给更保守的 1 天，因为它们没 cache-bust 机制。
    if resp.status_code == 200 and request.path.startswith("/static/"):
        if request.query_string:
            # cache-busted URL → 30 天
            resp.headers["Cache-Control"] = "public, max-age=2592000"
        else:
            resp.headers["Cache-Control"] = "public, max-age=86400"

    return resp


# ------------------------------------------------------------------ #
# Jinja 全局：过滤器 + CSRF + i18n + 鉴权状态
# ------------------------------------------------------------------ #
jinja_filters.register(app)
_csrf.register(app)
early_hints.register(app)


_ASSET_VERSIONS: dict[str, str] = {}


@app.template_global()
def asset(path: str) -> str:
    """/static 资源带上按文件内容算的版本号，例如 ``asset('design.css')``。

    以前每个模板自己写死 ``?v=34``，改了 CSS 就得记得手动 +1。两个后果都
    真实发生过：
    - 改了 app.js 忘了改版本号 → 用户拿到旧脚本，整个统计页空白；
    - login.html 有自己的一份 ``?v=28``，跟着 base.html 漏了 6 次 → 登录页
      （新访客看到的第一个页面）一直在发过期样式表。

    改成按 mtime+size 算摘要：文件一变版本号自动变，没有需要记住的步骤。
    结果进程内缓存，正常请求不碰磁盘；debug 模式下每次重算，方便边改边看。
    """
    if not app.debug and path in _ASSET_VERSIONS:
        return f"/static/{path}?v={_ASSET_VERSIONS[path]}"
    try:
        st = os.stat(os.path.join(app.static_folder or "static", path))
        digest = hashlib.sha1(f"{st.st_mtime_ns}-{st.st_size}".encode()).hexdigest()[:8]
    except OSError:
        # 文件不在（打包/部署出错）时不要连页面一起崩，退化成无版本号
        return f"/static/{path}"
    _ASSET_VERSIONS[path] = digest
    return f"/static/{path}?v={digest}"


@app.context_processor
def _inject_auth():
    return {
        "auth_enabled":    auth_enabled(),
        "is_admin":        is_admin(),
        "is_user":         is_user(),
        "current_user_id": current_user_id(),
        "guest_mode":      guest_mode_enabled(),
    }


@app.context_processor
def _inject_translations():
    lang = get_lang()

    def _(key: str) -> str:
        return _tr(key, lang)

    def dim_scope(dim: str) -> str:
        """该过滤维度只对部分平台生效时的提示；全平台通用时返回空串。

        平台不支持某维度时该条件对它整体跳过，界面上必须说出来——否则勾了
        「能耗 ≥ A」的用户会以为收到的都是 A 级，而 Xior 的房源一条都没过
        这一关。
        """
        from config import dim_scope_note
        return dim_scope_note(dim, lang)

    def dim_scope_badge(dim: str) -> str:
        """紧凑版标记，配合 dim_scope() 当 tooltip 使用。"""
        from config import dim_scope_badge as _badge
        return _badge(dim, lang)

    return {
        "_": _, "lang": lang,
        "dim_scope": dim_scope, "dim_scope_badge": dim_scope_badge,
    }


@app.context_processor
def _inject_upstream_maintenance():
    """
    向所有模板注入 H2S 平台维护态。

    base.html 用 `upstream_maintenance.active` 决定是否渲染顶部 banner。
    异常时静默——dashboard 永远不应该因为状态查询失败而崩。

    5s TTL 缓存：避免每次页面渲染都读 SQLite meta 表。
    """
    now = time.monotonic()
    cache = getattr(_inject_upstream_maintenance, "_cache", None)
    if cache is not None and (now - cache[0]) < 5:
        return {"upstream_maintenance": cache[1]}
    try:
        from app.services.monitor_service import get_upstream_maintenance
        info = get_upstream_maintenance()
    except Exception:
        info = {"active": "", "since": "", "last_seen": ""}
    _inject_upstream_maintenance._cache = (now, info)
    return {"upstream_maintenance": info}


# ------------------------------------------------------------------ #
# 路由：每个 app.routes.* 模块挂自己的 endpoint，扁平命名（A 方案）
# ------------------------------------------------------------------ #
sessions.register(app)         # /login /logout /guest /set-lang
dashboard.register(app)        # / /listings
users.register(app)            # /users*
email_verify_routes.register(app)  # /verify-email/<token> /users/<id>/resend-verify
settings_routes.register(app)  # /settings
map_routes.register(app)       # /map /api/map* /api/neighborhoods
calendar_routes.register(app)  # /calendar /api/calendar
stats.register(app)            # /stats /api/charts
system.register(app)           # /system /logs /api/logs* /api/status /api/platform /health /api/reset-db
control.register(app)          # /api/reload /api/monitor/{start,stop} /api/shutdown
notifications.register(app)    # /api/notifications* /api/events
inbound.register(app)          # /api/inbound/email （Resend webhook，Svix 签名校验，无需登录）
app_accounts.register(app)     # /settings/app-accounts (admin: Bearer token 管理)
legal.register(app)            # /privacy /terms （公开页面，无需登录）
site_meta.register(app)        # /robots.txt /favicon.ico /apple-touch-icon*（公开）
api_v1.register(app)           # /api/v1/auth/* /api/v1/stats/public/* (Bearer token)


# ------------------------------------------------------------------ #
# 入口
# ------------------------------------------------------------------ #

def main() -> None:
    # update_checker 触发一次网络请求，仅 CLI 直接运行时需要；
    # gunicorn / launcher 启动 web:app 时不会经过 main()，避免无谓的启动开销。
    from update_checker import check_for_updates

    parser = argparse.ArgumentParser(description="Holland2Stay Web 面板")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--debug", action="store_true", default=os.environ.get("FLASK_DEBUG", "").lower() in {"1", "true", "yes"})
    args = parser.parse_args()
    check_for_updates()
    print(f"Web 面板运行中 → http://{args.host}:{args.port}" + (" (debug)" if args.debug else ""))
    # threaded=True：允许多个 SSE 连接并发（每个连接占用一个线程）
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
