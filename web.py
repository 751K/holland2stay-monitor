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
import logging.handlers
import os
import sys
from pathlib import Path

from flask import Flask

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
from app import jinja_filters                                    # noqa: E402
from app.auth import (                                            # noqa: E402
    auth_enabled,
    ensure_secret_key,
    guest_mode_enabled,
    is_admin,
)
from app.i18n import get_lang                                     # noqa: E402
from app.routes import (                                          # noqa: E402
    calendar_routes,
    control,
    dashboard,
    map_routes,
    notifications,
    sessions,
    settings as settings_routes,
    stats,
    system,
    users,
)

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


@app.after_request
def _add_security_headers(resp):
    resp.headers.setdefault("X-Frame-Options",        "DENY")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy",        "strict-origin-when-cross-origin")
    return resp


# ------------------------------------------------------------------ #
# Jinja 全局：过滤器 + CSRF + i18n + 鉴权状态
# ------------------------------------------------------------------ #
jinja_filters.register(app)
_csrf.register(app)


@app.context_processor
def _inject_auth():
    return {
        "auth_enabled": auth_enabled(),
        "is_admin":     is_admin(),
        "guest_mode":   guest_mode_enabled(),
    }


@app.context_processor
def _inject_translations():
    lang = get_lang()

    def _(key: str) -> str:
        return _tr(key, lang)

    return {"_": _, "lang": lang}


# ------------------------------------------------------------------ #
# 路由：每个 app.routes.* 模块挂自己的 endpoint，扁平命名（A 方案）
# ------------------------------------------------------------------ #
sessions.register(app)         # /login /logout /guest /set-lang
dashboard.register(app)        # / /listings
users.register(app)            # /users*
settings_routes.register(app)  # /settings
map_routes.register(app)       # /map /api/map* /api/neighborhoods
calendar_routes.register(app)  # /calendar /api/calendar
stats.register(app)            # /stats /api/charts
system.register(app)           # /system /logs /api/logs* /api/status /api/platform /health /api/reset-db
control.register(app)          # /api/reload /api/monitor/{start,stop} /api/shutdown
notifications.register(app)    # /api/notifications* /api/events


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
    args = parser.parse_args()
    check_for_updates()
    print(f"Web 面板运行中 → http://{args.host}:{args.port}")
    # threaded=True：允许多个 SSE 连接并发（每个连接占用一个线程）
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
