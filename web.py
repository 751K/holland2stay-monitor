"""
Holland2Stay 监控 Web 面板
==========================
运行方式：
    python web.py               # 默认 http://localhost:5000
    python web.py --port 8080   # 自定义端口
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import secrets
import signal
import sys
import threading
import time as _time
import uuid
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any

from dotenv import dotenv_values, set_key
from flask import Flask, Response, flash, jsonify, redirect, render_template, request, session, stream_with_context, url_for

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import (  # noqa: E402
    BASE_DIR,
    DATA_DIR,
    ENV_PATH,
    KNOWN_CITIES,
    AutoBookConfig,
    ListingFilter,
    resolve_project_path,
)
from models import LISTING_KEY_MAP                               # noqa: E402
from storage import Storage                                      # noqa: E402
from users import UserConfig, get_user, load_users, save_users  # noqa: E402

# ------------------------------------------------------------------ #
# 常量
# ------------------------------------------------------------------ #

DB_PATH  = resolve_project_path(os.environ.get("DB_PATH", "data/listings.db"))
PID_FILE = DATA_DIR / "monitor.pid"
RELOAD_REQUEST_FILE = DATA_DIR / "monitor.reload"

# 全局配置可写入的 .env 键（通知/过滤/预订已移至 users.json）
_SETTINGS_KEYS = [
    "CHECK_INTERVAL", "LOG_LEVEL",
    # 智能轮询
    "PEAK_INTERVAL", "MIN_INTERVAL", "PEAK_START", "PEAK_END", "PEAK_WEEKDAYS_ONLY", "JITTER_RATIO",
]

# ------------------------------------------------------------------ #
# Flask app
# ------------------------------------------------------------------ #

app = Flask(__name__, template_folder="templates")

# SameSite=Lax：阻止跨站 POST 请求携带 session cookie（主要 CSRF 防护层）。
# HttpOnly=True：禁止 JS 读取 session cookie（Flask 默认已是 True，此处显式声明）。
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True

# 稳定的 secret key：优先读 .env，不存在则自动生成并写入，保证重启后 session 不失效
def _ensure_secret_key() -> str:
    key = os.environ.get("FLASK_SECRET", "")
    if key:
        return key
    key = secrets.token_hex(32)
    if ENV_PATH.exists() or not ENV_PATH.parent.exists():
        try:
            ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
            set_key(str(ENV_PATH), "FLASK_SECRET", key, quote_mode="never")
        except Exception:
            pass
    return key

app.secret_key = _ensure_secret_key()


# ------------------------------------------------------------------ #
# 鉴权
# ------------------------------------------------------------------ #

def _auth_enabled() -> bool:
    """只有 WEB_PASSWORD 已设置才开启鉴权（向后兼容：未设置时无需登录）。"""
    return bool(os.environ.get("WEB_PASSWORD", "").strip())


def _safe_next_url(candidate: str) -> str:
    """
    校验重定向目标，防止开放重定向（Open Redirect）攻击。

    规则
    ----
    只允许同源相对路径：必须以 "/" 开头，且不以 "//" 开头。
    - "/dashboard"         → ✅ 合法相对路径
    - "//evil.com/phish"   → ❌ 协议相对 URL，仍指向外部域名
    - "https://evil.com"   → ❌ 绝对 URL，指向外部域名
    - "javascript:alert()" → ❌ 非路径，不以 "/" 开头

    login_required 装饰器通过 next=request.path 注入，request.path
    始终是纯路径（以 "/" 开头，不含 host），是安全来源，此函数用于
    校验来自表单/查询参数（不可信来源）的 next 值。

    Parameters
    ----------
    candidate : 从请求参数中读取的原始 next 字符串

    Returns
    -------
    校验通过的路径原样返回；不通过时返回 "/" 首页路径
    """
    if candidate and candidate.startswith("/") and not candidate.startswith("//"):
        return candidate
    return url_for("index")


def login_required(f):
    """页面路由装饰器：未登录时跳转到登录页。"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if _auth_enabled() and not session.get("authenticated"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated


def api_login_required(f):
    """API 路由装饰器：未登录时返回 401 JSON。"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if _auth_enabled() and not session.get("authenticated"):
            return jsonify({"error": "unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


# ------------------------------------------------------------------ #
# CSRF 防护（纵深防御，配合 SameSite=Lax 双重保障）
# ------------------------------------------------------------------ #

def _get_csrf_token() -> str:
    """
    获取（或首次生成）绑定到当前 session 的 CSRF token。

    token 存储在 Flask session 中，随 session cookie 一起管理。
    每个 session 生成一次，不随请求更换（fixed-token 模式，足以防御 CSRF）。
    """
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)
    return session["csrf_token"]


def csrf_required(f):
    """
    路由装饰器：对 POST 请求验证 CSRF token。

    token 来源（任一即可）：
    - 表单字段  : csrf_token
    - 请求头    : X-CSRF-Token（fetch/XHR 调用使用）

    校验使用 hmac.compare_digest 防时序攻击。
    校验失败时返回 403，不泄露具体原因。
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.method == "POST":
            token = (request.form.get("csrf_token")
                     or request.headers.get("X-CSRF-Token", ""))
            expected = session.get("csrf_token", "")
            if not token or not expected or not hmac.compare_digest(token, expected):
                from flask import abort
                abort(403)
        return f(*args, **kwargs)
    return decorated


# 注册为 Jinja2 全局函数，模板中直接调用 csrf_token()
app.jinja_env.globals["csrf_token"] = _get_csrf_token


def _storage() -> Storage:
    return Storage(DB_PATH)


# ------------------------------------------------------------------ #
# Jinja2 工具
# ------------------------------------------------------------------ #

@app.context_processor
def _inject_auth():
    return {"auth_enabled": _auth_enabled()}


@app.template_filter("time_ago")
def time_ago(iso_str: str) -> str:
    if not iso_str or iso_str == "—":
        return "—"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        diff = datetime.now(timezone.utc) - dt
        secs = int(diff.total_seconds())
        if secs < 60:    return f"{secs}秒前"
        if secs < 3600:  return f"{secs // 60}分钟前"
        if secs < 86400: return f"{secs // 3600}小时前"
        return f"{secs // 86400}天前"
    except Exception:
        return iso_str


@app.template_filter("price_short")
def price_short(price_raw: str) -> str:
    if not price_raw:
        return "—"
    m = re.search(r"€[\d,\.]+", price_raw)
    return m.group() if m else price_raw


@app.template_filter("parse_features")
def parse_features(features_json: str) -> dict[str, str]:
    try:
        items = json.loads(features_json or "[]")
    except Exception:
        return {}
    result: dict[str, str] = {}
    for feat in items:
        if ": " in feat:
            raw_key, val = feat.split(": ", 1)
            result[LISTING_KEY_MAP.get(raw_key, raw_key.lower())] = val
    return result


@app.template_global()
def status_badge(status: str) -> str:
    s = status.lower()
    if "book" in s:    return "success"
    if "lottery" in s: return "warning"
    return "secondary"


# ------------------------------------------------------------------ #
# 路由 — 登录 / 登出
# ------------------------------------------------------------------ #

@app.route("/login", methods=["GET", "POST"])
@csrf_required
def login() -> Any:
    # 如果鉴权未启用，直接跳首页
    if not _auth_enabled():
        return redirect(url_for("index"))
    # 已登录也跳首页
    if session.get("authenticated"):
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        expected_user = os.environ.get("WEB_USERNAME", "admin")
        expected_pass = os.environ.get("WEB_PASSWORD", "")

        # 用 hmac.compare_digest 防时序攻击
        user_ok = hmac.compare_digest(username, expected_user)
        pass_ok = hmac.compare_digest(password, expected_pass)
        if user_ok and pass_ok:
            session.permanent = True
            session["authenticated"] = True
            next_url = _safe_next_url(request.form.get("next", ""))
            return redirect(next_url)

        flash("用户名或密码错误", "danger")

    return render_template("login.html", next=request.args.get("next", ""),
                           auth_enabled=_auth_enabled())


@app.route("/logout", methods=["POST"])
@csrf_required
def logout() -> Any:
    session.clear()
    return redirect(url_for("login"))


# ------------------------------------------------------------------ #
# 路由 — 仪表盘 & 房源
# ------------------------------------------------------------------ #

@app.route("/")
@login_required
def index() -> str:
    storage = _storage()
    try:
        last_scrape = storage.get_meta("last_scrape_at")
        stats = {
            "total":       storage.count_all(),
            "new_24h":     storage.count_new_since(hours=24),
            "changes_24h": storage.count_changes_since(hours=24),
            "last_scrape": last_scrape,
            "last_count":  storage.get_meta("last_scrape_count"),
        }
        recent  = storage.get_all_listings(limit=15)
        changes = storage.get_recent_changes(hours=48)
    finally:
        storage.close()
    return render_template("index.html", stats=stats, recent=recent, changes=changes)


@app.route("/listings")
@login_required
def listings() -> str:
    status_filter = request.args.get("status", "")
    search        = request.args.get("q", "")
    storage = _storage()
    try:
        rows     = storage.get_all_listings(status=status_filter or None, search=search or None, limit=500)
        statuses = storage.get_distinct_statuses()
    finally:
        storage.close()
    return render_template(
        "listings.html",
        listings=rows, statuses=statuses,
        status_filter=status_filter, search=search,
    )


# ------------------------------------------------------------------ #
# 路由 — 设置（全局配置）
# ------------------------------------------------------------------ #

@app.route("/settings", methods=["GET", "POST"])
@login_required
@csrf_required
def settings() -> Any:
    if request.method == "POST":
        if not ENV_PATH.exists():
            ENV_PATH.touch()

        # 城市：复选框提交 "CityName,ID" 格式，用 | 拼接
        selected_cities = request.form.getlist("city_selected")
        cities_val = "|".join(selected_cities) if selected_cities else "Eindhoven,29"
        set_key(str(ENV_PATH), "CITIES", cities_val, quote_mode="never")

        for key in _SETTINGS_KEYS:
            val = request.form.get(key, "")
            set_key(str(ENV_PATH), key, val, quote_mode="never")

        flash("✅ 全局配置已保存", "success")
        return redirect(url_for("settings"))

    env = dict(dotenv_values(str(ENV_PATH)))

    selected_city_ids: set[str] = set()
    for entry in env.get("CITIES", "Eindhoven,29").split("|"):
        parts = entry.strip().split(",")
        if len(parts) >= 2:
            selected_city_ids.add(parts[-1].strip())

    return render_template(
        "settings.html",
        env=env,
        known_cities=KNOWN_CITIES,
        selected_city_ids=selected_city_ids,
    )


# ------------------------------------------------------------------ #
# 路由 — 用户管理
# ------------------------------------------------------------------ #

def _user_from_form(
    form,
    user_id: str | None = None,
    existing: "UserConfig | None" = None,
) -> "UserConfig":
    """
    从表单数据构建 UserConfig。

    Parameters
    ----------
    form     : request.form（ImmutableMultiDict）
    user_id  : 编辑模式时传入已有 ID，新建时传 None（自动生成）
    existing : 编辑模式时传入当前 UserConfig 对象，用于在密码字段为空时
               保留旧密码而不是将其清空。
               密码字段在 GET 时不回填到 HTML，空提交 = "不修改"。
               新建模式传 None，空密码字段即存为空字符串。
    """
    def _fv(key: str):
        v = form.get(key, "").strip()
        return float(v) if v else None

    def _iv(key: str):
        v = form.get(key, "").strip()
        return int(v) if v else None

    def _lv(key: str) -> list[str]:
        v = form.get(key, "").strip()
        return [x.strip() for x in v.split(",") if x.strip()] if v else []

    def _secret(key: str, old_val: str) -> str:
        """
        密码/令牌字段的安全读取：
        - 表单字段非空 → 使用新值（用户正在更新密码）
        - 表单字段为空 → 保留 old_val（用户未动密码字段，不清除已保存的值）
        """
        v = form.get(key, "").strip()
        return v if v else old_val

    channels_raw = form.get("NOTIFICATION_CHANNELS", "")
    channels = [c.strip().lower() for c in channels_raw.split(",") if c.strip()]

    lf = ListingFilter(
        max_rent=_fv("MAX_RENT"),
        min_area=_fv("MIN_AREA"),
        max_area=_fv("MAX_AREA"),
        min_floor=_iv("MIN_FLOOR"),
        allowed_occupancy=_lv("ALLOWED_OCCUPANCY"),
        allowed_types=_lv("ALLOWED_TYPES"),
        allowed_neighborhoods=_lv("ALLOWED_NEIGHBORHOODS"),
    )

    ex_ab = existing.auto_book if existing else None
    ab = AutoBookConfig(
        enabled=form.get("AUTO_BOOK_ENABLED") == "true",
        dry_run=form.get("AUTO_BOOK_DRY_RUN", "true") != "false",
        email=form.get("AUTO_BOOK_EMAIL", ""),
        password=_secret("AUTO_BOOK_PASSWORD", ex_ab.password if ex_ab else ""),
        listing_filter=ListingFilter(
            max_rent=_fv("AUTO_BOOK_MAX_RENT"),
            min_area=_fv("AUTO_BOOK_MIN_AREA"),
            max_area=_fv("AUTO_BOOK_MAX_AREA"),
            min_floor=_iv("AUTO_BOOK_MIN_FLOOR"),
            allowed_occupancy=_lv("AUTO_BOOK_ALLOWED_OCCUPANCY"),
            allowed_types=_lv("AUTO_BOOK_ALLOWED_TYPES"),
            allowed_neighborhoods=_lv("AUTO_BOOK_ALLOWED_NEIGHBORHOODS"),
        ),
    )
    return UserConfig(
        id=user_id or uuid.uuid4().hex[:8],
        name=form.get("name", "").strip() or "未命名用户",
        enabled=form.get("enabled") == "true",
        notifications_enabled=form.get("NOTIFICATIONS_ENABLED", "true") != "false",
        notification_channels=channels,
        imessage_recipient=form.get("IMESSAGE_RECIPIENT", ""),
        telegram_token=form.get("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=form.get("TELEGRAM_CHAT_ID", ""),
        email_smtp_host=form.get("EMAIL_SMTP_HOST", "").strip(),
        email_smtp_port=_iv("EMAIL_SMTP_PORT") or 587,
        email_smtp_security=form.get("EMAIL_SMTP_SECURITY", "starttls").strip().lower() or "starttls",
        email_username=form.get("EMAIL_USERNAME", "").strip(),
        email_password=_secret("EMAIL_PASSWORD", existing.email_password if existing else ""),
        email_from=form.get("EMAIL_FROM", "").strip(),
        email_to=form.get("EMAIL_TO", "").strip(),
        twilio_sid=form.get("TWILIO_ACCOUNT_SID", ""),
        twilio_token=_secret("TWILIO_AUTH_TOKEN", existing.twilio_token if existing else ""),
        twilio_from=form.get("TWILIO_FROM", ""),
        twilio_to=form.get("TWILIO_TO", ""),
        listing_filter=lf,
        auto_book=ab,
    )


@app.route("/users")
@login_required
def users_list() -> str:
    users = load_users()
    return render_template("users.html", users=users)


@app.route("/users/new", methods=["GET", "POST"])
@login_required
@csrf_required
def user_new() -> Any:
    if request.method == "POST":
        user = _user_from_form(request.form)
        users = load_users()
        users.append(user)
        save_users(users)
        flash(f"✅ 用户「{user.name}」已创建", "success")
        return redirect(url_for("users_list"))
    # GET：空白表单
    return render_template("user_form.html", user=None,
                           action=url_for("user_new"), title="新增用户")


@app.route("/users/<user_id>", methods=["GET", "POST"])
@login_required
@csrf_required
def user_edit(user_id: str) -> Any:
    users = load_users()
    user = get_user(users, user_id)
    if user is None:
        flash("用户不存在", "danger")
        return redirect(url_for("users_list"))

    if request.method == "POST":
        # existing=user 确保空密码字段保留旧值，不会意外清除已保存的密码
        updated = _user_from_form(request.form, user_id=user_id, existing=user)
        users = [updated if u.id == user_id else u for u in users]
        save_users(users)
        flash(f"✅ 用户「{updated.name}」已保存", "success")
        return redirect(url_for("user_edit", user_id=user_id))

    return render_template("user_form.html", user=user,
                           action=url_for("user_edit", user_id=user_id),
                           title=f"编辑用户 · {user.name}")


@app.route("/users/<user_id>/delete", methods=["POST"])
@login_required
@csrf_required
def user_delete(user_id: str) -> Any:
    users = load_users()
    user = get_user(users, user_id)
    name = user.name if user else user_id
    users = [u for u in users if u.id != user_id]
    save_users(users)
    flash(f"用户「{name}」已删除", "success")
    return redirect(url_for("users_list"))


@app.route("/users/<user_id>/test", methods=["POST"])
@login_required
@csrf_required
def user_test_notify(user_id: str) -> Any:
    """逐渠道发送一条测试消息，返回每个渠道的成功/失败详情。"""
    import asyncio
    from datetime import datetime as _dt
    from notifier import EmailNotifier, IMessageNotifier, TelegramNotifier, WhatsAppNotifier

    users = load_users()
    user = get_user(users, user_id)
    if user is None:
        return jsonify({"ok": False, "error": "用户不存在"}), 404

    test_msg = (
        f"🧪 Holland2Stay 监控\n\n"
        f"这是一条通知测试消息\n"
        f"发送时间：{_dt.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"通知配置正确 ✅"
    )

    results: list[dict] = []

    for channel in user.notification_channels:
        ch = channel.strip().lower()

        if ch == "imessage":
            if not user.imessage_recipient:
                results.append({"channel": "iMessage", "ok": False, "error": "收件人未配置"})
                continue
            notifier_obj = IMessageNotifier(user.imessage_recipient)
            label = f"iMessage → {user.imessage_recipient}"

        elif ch == "telegram":
            if not user.telegram_token or not user.telegram_chat_id:
                results.append({"channel": "Telegram", "ok": False, "error": "Token 或 Chat ID 未配置"})
                continue
            notifier_obj = TelegramNotifier(user.telegram_token, user.telegram_chat_id)
            label = f"Telegram → {user.telegram_chat_id}"

        elif ch == "email":
            has_auth = bool(user.email_username or user.email_password)
            if not user.email_smtp_host or not user.email_to or not (user.email_from or user.email_username):
                results.append({"channel": "Email", "ok": False, "error": "SMTP 主机、发件人或收件人未配置"})
                continue
            if has_auth and not (user.email_username and user.email_password):
                results.append({"channel": "Email", "ok": False, "error": "SMTP 用户名和密码需要同时填写"})
                continue
            notifier_obj = EmailNotifier(
                user.email_smtp_host,
                user.email_smtp_port,
                user.email_smtp_security,
                user.email_username,
                user.email_password,
                user.email_from,
                user.email_to,
            )
            label = f"Email → {user.email_to}"

        elif ch == "whatsapp":
            if not all([user.twilio_sid, user.twilio_token, user.twilio_from, user.twilio_to]):
                results.append({"channel": "WhatsApp", "ok": False, "error": "Twilio 参数不完整"})
                continue
            notifier_obj = WhatsAppNotifier(
                user.twilio_sid, user.twilio_token, user.twilio_from, user.twilio_to
            )
            label = f"WhatsApp → {user.twilio_to}"

        else:
            results.append({"channel": ch, "ok": False, "error": "未知渠道"})
            continue

        try:
            ok = asyncio.run(notifier_obj._send(test_msg))
            results.append({"channel": label, "ok": ok,
                            "error": None if ok else "发送失败，请检查日志"})
        except Exception as e:
            results.append({"channel": label, "ok": False, "error": str(e)})

    if not results:
        return jsonify({"ok": False, "results": [], "error": "该用户未配置任何通知渠道"})

    return jsonify({"ok": any(r["ok"] for r in results), "results": results})


@app.route("/users/<user_id>/toggle", methods=["POST"])
@login_required
@csrf_required
def user_toggle(user_id: str) -> Any:
    """快速开关用户启用状态。"""
    users = load_users()
    for u in users:
        if u.id == user_id:
            u.enabled = not u.enabled
            break
    save_users(users)
    return redirect(url_for("users_list"))


# ------------------------------------------------------------------ #
# 路由 — 入住日历
# ------------------------------------------------------------------ #

@app.route("/calendar")
@login_required
def calendar() -> str:
    return render_template("calendar.html")


@app.route("/api/calendar")
@api_login_required
def api_calendar():
    """返回所有有入住日期的房源，供日历前端渲染。"""
    storage = _storage()
    try:
        listings = storage.get_calendar_listings()
    finally:
        storage.close()
    return jsonify({"listings": listings})


# ------------------------------------------------------------------ #
# 路由 — 统计图表
# ------------------------------------------------------------------ #

@app.route("/stats")
@login_required
def stats() -> str:
    storage = _storage()
    try:
        total    = storage.count_all()
        new_24h  = storage.count_new_since(hours=24)
        new_7d   = storage.count_new_since(hours=24 * 7)
        changes_24h = storage.count_changes_since(hours=24)
    finally:
        storage.close()
    return render_template(
        "stats.html",
        total=total, new_24h=new_24h, new_7d=new_7d, changes_24h=changes_24h,
    )


@app.route("/api/charts")
@api_login_required
def api_charts():
    """所有图表数据的 JSON API，供前端 Chart.js 调用。"""
    try:
        days = int(request.args.get("days", 30))
    except (TypeError, ValueError):
        days = 30
    days = max(1, min(days, 365))  # 限制在 [1, 365]，防止超大查询
    storage = _storage()
    try:
        data = {
            "daily_new":     storage.chart_daily_new(days=days),
            "daily_changes": storage.chart_daily_changes(days=days),
            "city_dist":     storage.chart_city_dist(),
            "status_dist":   storage.chart_status_dist(),
            "price_dist":    storage.chart_price_dist(),
        }
    finally:
        storage.close()
    return jsonify(data)


# ------------------------------------------------------------------ #
# API
# ------------------------------------------------------------------ #

def _pid_exists(pid: int) -> bool:
    """
    跨平台检查 PID 是否仍然存活。

    - POSIX: 使用 `os.kill(pid, 0)`
    - Windows: 使用 Win32 `OpenProcess + GetExitCodeProcess`

    说明
    ----
    Windows 上 `os.kill(pid, 0)` 并不可靠，某些场景会抛出
    `OSError: [WinError 11]`（截图中的问题），因此需要单独走 WinAPI。
    """
    if pid <= 0:
        return False

    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False
        except OSError:
            return False

    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            return bool(ok and exit_code.value == STILL_ACTIVE)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False


def _monitor_pid() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
        return pid if _pid_exists(pid) else None
    except ValueError:
        return None


def _write_reload_request() -> None:
    """写入文件触发的热重载请求，供 Windows 和信号失败场景使用。"""
    RELOAD_REQUEST_FILE.parent.mkdir(parents=True, exist_ok=True)
    RELOAD_REQUEST_FILE.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")


@app.route("/api/status")
@api_login_required
def api_status():
    pid = _monitor_pid()
    users = load_users()
    return jsonify({
        "running": pid is not None,
        "pid": pid,
        "users": len(users),
        "active_users": sum(1 for u in users if u.enabled),
    })


@app.route("/api/reload", methods=["POST"])
@api_login_required
@csrf_required
def api_reload():
    pid = _monitor_pid()
    if pid is None:
        return jsonify({"ok": False, "error": "监控程序未运行，请先启动 monitor.py"}), 400

    # Windows 没有可靠的 SIGHUP 语义，统一改为写入 reload 请求文件。
    # 监控进程会在等待间隙轮询该文件并提前热重载。
    if os.name == "nt" or not hasattr(signal, "SIGHUP"):
        try:
            _write_reload_request()
            return jsonify({"ok": True, "message": "已写入重载请求，配置将在 1 秒内检测并生效"})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    try:
        os.kill(pid, signal.SIGHUP)
        return jsonify({"ok": True, "message": "重载信号已发送，配置将在本轮抓取结束后生效"})
    except Exception:
        # 回退到文件触发机制，避免因信号发送失败导致 Web 面板无法应用配置。
        try:
            _write_reload_request()
            return jsonify({"ok": True, "message": "信号发送失败，已回退为文件触发重载"})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500


# ------------------------------------------------------------------ #
# API — Web 通知
# ------------------------------------------------------------------ #

@app.route("/api/notifications")
@api_login_required
def api_notifications():
    """
    分页查询 Web 通知列表。

    Query params
    ------------
    limit  : 每页条数，默认 50，最大 200
    offset : 分页偏移，默认 0
    """
    try:
        limit  = min(int(request.args.get("limit",  50)), 200)
        offset = max(int(request.args.get("offset",  0)),   0)
    except (TypeError, ValueError):
        limit, offset = 50, 0

    storage = _storage()
    try:
        rows   = storage.get_notifications(limit=limit, offset=offset)
        unread = storage.count_unread_notifications()
    finally:
        storage.close()

    return jsonify({"ok": True, "notifications": rows, "unread": unread})


@app.route("/api/notifications/read", methods=["POST"])
@api_login_required
@csrf_required
def api_notifications_read():
    """标记所有通知为已读（或指定 ids 数组）。"""
    data = request.get_json(silent=True) or {}
    ids  = data.get("ids")  # None → 全部；list[int] → 指定

    storage = _storage()
    try:
        storage.mark_notifications_read(ids=ids)
    finally:
        storage.close()

    return jsonify({"ok": True})


@app.route("/api/events")
@api_login_required
def api_events():
    """
    SSE（Server-Sent Events）端点，每 5 秒推送增量通知给浏览器。

    浏览器通过 EventSource('/api/events?last_id=N') 订阅。
    若有新通知，发送 `data: <JSON数组>\\n\\n`；否则发送保活注释 `: keepalive\\n\\n`。

    线程泄漏防护（三层）
    --------------------
    Gunicorn sync worker 在客户端断连后不一定向生成器注入 GeneratorExit——
    生成器阻塞在 time.sleep() 期间无法接收任何信号，线程因此永久堆积。

    1. **yield 处捕获写入异常**
       BrokenPipeError / ConnectionResetError / OSError 表示客户端已断连，
       任一异常立即 return，不再进入下一轮循环。

    2. **可中断 sleep（threading.Event.wait）**
       用 stop.wait(5) 替代 time.sleep(5)：
       当写入异常退出时，finally 块调用 stop.set()，
       若此时另一个线程（或同一线程的其他路径）正在 wait()，立即唤醒。

    3. **硬性最大连接时长（_SSE_MAXAGE = 300 s）**
       到期后主动关闭连接；浏览器 EventSource 按 `retry: 2000`（2 s）
       自动重连，对用户完全透明。

    Gunicorn 推荐部署
    -----------------
    gevent/eventlet worker 可协作式处理断连，无需以上防护：
        gunicorn -k gevent --worker-connections 1000 web:app
    """
    _SSE_POLL   = 5    # 轮询间隔（秒）
    _SSE_MAXAGE = 300  # 单次连接最大生命周期（秒），到期后让浏览器重连

    try:
        last_id = int(request.args.get("last_id", 0))
    except (TypeError, ValueError):
        last_id = 0

    stop    = threading.Event()
    expires = _time.monotonic() + _SSE_MAXAGE

    def _generate():
        nonlocal last_id
        # 告知浏览器 2 s 后重连（连接到期或异常关闭时生效）
        yield "retry: 2000\n\n"
        try:
            while not stop.is_set() and _time.monotonic() < expires:
                st = _storage()
                try:
                    rows = st.get_notifications_since(last_id)
                finally:
                    st.close()

                if rows:
                    last_id = rows[-1]["id"]
                    payload = json.dumps(rows, ensure_ascii=False)
                    chunk = f"data: {payload}\n\n"
                else:
                    chunk = ": keepalive\n\n"

                try:
                    yield chunk
                except (GeneratorExit, BrokenPipeError, ConnectionResetError, OSError):
                    # 写入失败 = 客户端已断连，立即退出，不再轮询
                    return

                # 可中断 sleep：stop.set() 后立即唤醒，无需等满 _SSE_POLL 秒
                stop.wait(_SSE_POLL)
        except GeneratorExit:
            pass
        finally:
            stop.set()  # 确保所有退出路径都能唤醒任何正在等待的 stop.wait()

    return Response(
        stream_with_context(_generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # 禁用 Nginx 缓冲，确保实时推送
            "Connection": "keep-alive",
        },
    )


@app.route("/api/platform")
@api_login_required
def api_platform():
    """返回服务器平台信息，用于面板判断 iMessage 是否可用。"""
    import sys as _sys
    return jsonify({"macos": _sys.platform == "darwin", "platform": _sys.platform})


# ------------------------------------------------------------------ #
# 入口
# ------------------------------------------------------------------ #

def main() -> None:
    parser = argparse.ArgumentParser(description="Holland2Stay Web 面板")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    print(f"Web 面板运行中 → http://{args.host}:{args.port}")
    # threaded=True：允许多个 SSE 连接并发（每个连接占用一个线程）
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
