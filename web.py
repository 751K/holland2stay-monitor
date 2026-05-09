"""
Holland2Stay 监控 Web 面板
==========================
运行方式：
    python web.py               # 本地开发，默认 http://localhost:8088
    python web.py --port 8080   # 自定义端口

Docker 容器中由 Gunicorn 启动（supervisord.conf）：
    gunicorn --workers=1 --threads=8 --timeout=0 --bind=0.0.0.0:8088 web:app
    （直接运行 python web.py 仅用于本地调试）
"""
from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
import signal
import subprocess
import sys
import threading
import time as _time
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from flask import Flask, Response, flash, jsonify, redirect, render_template, request, session, stream_with_context, url_for

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import (  # noqa: E402
    ASSETS_DIR,
    BASE_DIR,
    DATA_DIR,
    DB_PATH,
    ENV_PATH,
    KNOWN_CITIES,
    TIMEZONE,
)
from storage import Storage                                      # noqa: E402
from translations import tr as _tr                                       # noqa: E402
from users import get_user, load_users, save_users  # noqa: E402

# app/ 子包：Stage 1+2 抽离的内聚模块，无 Flask 耦合或仅依赖请求上下文
from app import csrf as _csrf                                    # noqa: E402
from app import jinja_filters                                    # noqa: E402
from app.auth import (                                            # noqa: E402
    admin_api_required,
    admin_required,
    api_login_required,
    auth_enabled as _auth_enabled,
    check_login_rate as _check_login_rate,
    clear_login_failures as _clear_login_failures,
    ensure_secret_key as _ensure_secret_key,
    guest_mode_enabled as _guest_mode_enabled,
    is_admin as _is_admin,
    login_required,
    record_login_failure as _record_login_failure,
)
from app.csrf import csrf_required, get_csrf_token as _get_csrf_token  # noqa: E402
from app.env_writer import write_env_key as _write_env_key        # noqa: E402
from app.forms.user_form import build_user_from_form as _user_from_form  # noqa: E402
from app.i18n import (                                            # noqa: E402
    DEFAULTS as _DEFAULTS,
    get_lang as _get_lang,
    localize_options as _localize_options,
)
from app.process_ctrl import (                                    # noqa: E402
    PID_FILE,
    RELOAD_REQUEST_FILE,
    monitor_pid as _monitor_pid,
    pid_exists as _pid_exists,
    write_reload_request as _write_reload_request,
)
from app.safety import safe_next_url as _safe_next_url, sanitize_dotenv as _sanitize_dotenv  # noqa: E402

# ------------------------------------------------------------------ #
# 常量
# ------------------------------------------------------------------ #

TZ = TIMEZONE   # 本地别名，保持既有代码可读性
# PID_FILE / RELOAD_REQUEST_FILE 由 app.process_ctrl 提供（已在顶部 import）

# 全局配置可写入的 .env 键（通知/过滤/预订已移至 users.json）
_SETTINGS_KEYS = [
    "CHECK_INTERVAL", "LOG_LEVEL",
    # 智能轮询
    "PEAK_INTERVAL", "MIN_INTERVAL", "PEAK_START", "PEAK_END", "PEAK_WEEKDAYS_ONLY", "JITTER_RATIO",
]

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
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true"
app.config["PERMANENT_SESSION_LIFETIME"] = int(os.environ.get("SESSION_LIFETIME_HOURS", "24")) * 3600

# _write_env_key / _ensure_secret_key 由 app.env_writer / app.auth 提供（已在顶部 import）
app.secret_key = _ensure_secret_key()


@app.after_request
def _add_security_headers(resp):
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return resp


# ------------------------------------------------------------------ #
# 鉴权 + CSRF：装饰器和状态查询函数由 app.auth / app.csrf 提供（已在顶部 import）
# ------------------------------------------------------------------ #
_csrf.register(app)


def _storage() -> Storage:
    return Storage(DB_PATH, timezone_str=TZ)


# ------------------------------------------------------------------ #
# Jinja2 工具
# ------------------------------------------------------------------ #

@app.context_processor
def _inject_auth():
    return {
        "auth_enabled":  _auth_enabled(),
        "is_admin":      _is_admin(),
        "guest_mode":    _guest_mode_enabled(),
    }


# _get_lang 由 app.i18n 提供（已在顶部 as _get_lang 导入）。
# Jinja 过滤器 time_ago / price_short / parse_features / status_badge
# 现集中在 app.jinja_filters，并通过 jinja_filters.register(app) 一次性挂载。
jinja_filters.register(app)


@app.context_processor
def _inject_translations():
    lang = _get_lang()

    def _(key: str) -> str:
        return _tr(key, lang)

    return {"_": _, "lang": lang}


# ------------------------------------------------------------------ #
# 路由 — 语言切换
# ------------------------------------------------------------------ #

@app.route("/set-lang")
def set_lang() -> Any:
    lang = request.args.get("lang", "zh")
    if lang not in ("zh", "en"):
        lang = "zh"
    resp = redirect(_safe_next_url(request.args.get("next", "")))
    resp.set_cookie("h2s-lang", lang, max_age=60 * 60 * 24 * 365, samesite="Lax")
    return resp


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
        # 爆破防护：连续失败超阈值后指数退避
        client_ip = request.remote_addr or "0.0.0.0"
        delay = _check_login_rate(client_ip)
        if delay > 0:
            _time.sleep(delay)

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        # WEB_USERNAME 未设置时默认为 "admin"
        expected_user = os.environ.get("WEB_USERNAME", "").strip() or "admin"
        expected_pass = os.environ.get("WEB_PASSWORD", "")

        # 用 hmac.compare_digest 防时序攻击
        user_ok = hmac.compare_digest(username, expected_user)
        pass_ok = hmac.compare_digest(password, expected_pass)
        if user_ok and pass_ok:
            _clear_login_failures(client_ip)  # 成功则清除失败记录
            session.permanent = True
            session["authenticated"] = True
            session["role"] = "admin"
            next_url = _safe_next_url(request.form.get("next", ""))
            return redirect(next_url)

        _record_login_failure(client_ip)
        flash("用户名或密码错误", "danger")

    return render_template("login.html", next=request.args.get("next", ""),
                           auth_enabled=_auth_enabled(),
                           guest_mode=_guest_mode_enabled())


@app.route("/guest")
def guest_login() -> Any:
    """访客模式：无需密码，直接以只读身份进入面板。"""
    if not _auth_enabled():
        return redirect(url_for("index"))
    if not _guest_mode_enabled():
        return redirect(url_for("login"))
    # 已登录的 admin 不允许被降级为 guest（防止误操作或 CSRF 降级攻击）
    if session.get("role") == "admin":
        return redirect(url_for("index"))
    session.permanent = True
    session["authenticated"] = True
    session["role"] = "guest"
    return redirect(url_for("index"))


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
    city_filter = request.args.get("city", "")
    storage = _storage()
    try:
        all_cities = sorted({r["city"] for r in storage.get_all_listings(limit=2000) if r.get("city")})
        last_scrape = storage.get_meta("last_scrape_at")
        stats = {
            "total":       storage.count_all(city=city_filter or None),
            "new_24h":     storage.count_new_since(hours=24, city=city_filter or None),
            "changes_24h": storage.count_changes_since(hours=24, city=city_filter or None),
            "last_scrape": last_scrape,
            "last_count":  storage.get_meta("last_scrape_count"),
        }
        recent  = storage.get_all_listings(city=city_filter or None, limit=15)
        changes = storage.get_recent_changes(hours=48, city=city_filter or None)
    finally:
        storage.close()
    return render_template("index.html", stats=stats, recent=recent, changes=changes,
                           monitor_running=_monitor_pid() is not None,
                           city_filter=city_filter, all_cities=all_cities)


@app.route("/listings")
@login_required
def listings() -> str:
    from models import parse_features_list, parse_float
    import json as _json
    status_filter = request.args.get("status", "")
    name_query    = request.args.get("q", "")
    city_filter   = request.args.get("city", "")
    max_rent_str  = request.args.get("max_rent", "")
    min_area_str  = request.args.get("min_area", "")
    max_rent = parse_float(max_rent_str) if max_rent_str.strip() else None
    min_area = parse_float(min_area_str) if min_area_str.strip() else None
    storage = _storage()
    try:
        rows     = storage.get_all_listings(status=status_filter or None, search=name_query or None, city=city_filter or None, limit=500)
        statuses = storage.get_distinct_statuses()
        city_list = storage.get_distinct_cities()
    finally:
        storage.close()
    # Python 端租金/面积过滤（数据量小，无需 SQL 复杂度）
    if max_rent is not None:
        rows = [r for r in rows if (pv := parse_float(r.get("price_raw", ""))) is not None and pv <= max_rent]
    if min_area is not None:
        def _get_area(r):
            fm = parse_features_list(_json.loads(r.get("features", "[]")))
            return parse_float(fm.get("area", ""))
        rows = [r for r in rows if (a := _get_area(r)) is not None and a >= min_area]
    return render_template(
        "listings.html",
        listings=rows, statuses=statuses,
        status_filter=status_filter, search=name_query, city_filter=city_filter,
        cities=city_list, max_rent=max_rent_str, min_area=min_area_str,
    )


# ------------------------------------------------------------------ #
# 路由 — 设置（全局配置）
# ------------------------------------------------------------------ #

@app.route("/settings", methods=["GET", "POST"])
@admin_required
@csrf_required
def settings() -> Any:
    if request.method == "POST":
        if not ENV_PATH.exists():
            ENV_PATH.touch()

        # 城市：复选框提交 "CityName,ID" 格式，用 | 拼接
        selected_cities = request.form.getlist("city_selected")
        cities_val = "|".join(selected_cities) if selected_cities else "Eindhoven,29"
        _write_env_key("CITIES", _sanitize_dotenv(cities_val))

        for key in _SETTINGS_KEYS:
            val = request.form.get(key, "")
            _write_env_key(key, _sanitize_dotenv(val))

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
# _user_from_form 由 app.forms.user_form 提供（已在顶部 import）


@app.route("/users")
@admin_required
def users_list() -> str:
    users = load_users()
    return render_template("users.html", users=users)


# _DEFAULTS / _LABELS / _localize_options 由 app.i18n 提供（已在顶部 import）。
# 此处无需重复定义。


def _get_all_filter_options() -> dict[str, list[str]]:
    """一次 Storage 调用取所有过滤分类值，DB 为空时按分类回退预设。
    供 user_new / user_edit 使用，避免每个分类单独开关一次连接。"""
    st = _storage()
    try:
        return {
            cat: (st.get_feature_values(cat) or _DEFAULTS.get(cat, []))
            for cat in _DEFAULTS
        }
    except Exception:
        return {cat: vals for cat, vals in _DEFAULTS.items()}
    finally:
        st.close()


@app.route("/users/new", methods=["GET", "POST"])
@admin_required
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
    city_names = sorted(c["name"] for c in KNOWN_CITIES)
    opts = _get_all_filter_options()
    return render_template("user_form.html", user=None,
                           action=url_for("user_new"), title="新增用户",
                           occupancy_options=_localize_options("Occupancy", opts["Occupancy"]),
                           type_options=_localize_options("Type", opts["Type"]),
                           city_options=city_names,
                           contract_options=_localize_options("Contract", opts["Contract"]),
                           tenant_options=_localize_options("Tenant", opts["Tenant"]),
                           offer_options=opts["Offer"])


@app.route("/users/<user_id>", methods=["GET", "POST"])
@admin_required
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

    city_names = sorted(c["name"] for c in KNOWN_CITIES)
    opts = _get_all_filter_options()
    return render_template("user_form.html", user=user,
                           action=url_for("user_edit", user_id=user_id),
                           title=f"编辑用户 · {user.name}",
                           occupancy_options=_localize_options("Occupancy", opts["Occupancy"]),
                           type_options=_localize_options("Type", opts["Type"]),
                           city_options=city_names,
                           contract_options=_localize_options("Contract", opts["Contract"]),
                           tenant_options=_localize_options("Tenant", opts["Tenant"]),
                           offer_options=opts["Offer"])


@app.route("/users/<user_id>/delete", methods=["POST"])
@admin_required
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
@admin_required
@csrf_required
def user_test_notify(user_id: str) -> Any:
    """逐渠道发送一条测试消息，返回每个渠道的成功/失败详情。"""
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

    async def _send_and_close(notifier_obj: Any, msg: str) -> bool:
        """发送测试消息，无论成功与否都确保关闭 notifier（释放 curl_cffi Session 等）。"""
        try:
            return await notifier_obj._send(msg)
        finally:
            await notifier_obj.close()

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
            ok = asyncio.run(_send_and_close(notifier_obj, test_msg))
            results.append({"channel": label, "ok": ok,
                            "error": None if ok else "发送失败，请检查日志"})
        except Exception as e:
            results.append({"channel": label, "ok": False, "error": str(e)})

    if not results:
        return jsonify({"ok": False, "results": [], "error": "该用户未配置任何通知渠道"})

    return jsonify({"ok": any(r["ok"] for r in results), "results": results})


@app.route("/users/<user_id>/toggle", methods=["POST"])
@admin_required
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
# 路由 — 地图
# ------------------------------------------------------------------ #

_geocode_lock = threading.Lock()
_geocode_status = {"running": False, "total": 0, "done": 0, "failed": 0}


def _run_geocode_worker(addresses: list[str]) -> None:
    """后台线程：逐个地理编码地址列表，结果写入缓存，进度更新到全局状态。"""
    import time as _time
    from urllib.request import Request, urlopen
    from urllib.parse import quote

    st = _storage()
    done, failed = 0, 0
    try:
        for addr in addresses:
            try:
                url = f"https://photon.komoot.io/api/?q={quote(addr)}&limit=1"
                req = Request(url, headers={"User-Agent": "Holland2StayMonitor/1.0"})
                resp = urlopen(req, timeout=5)
                data = json.loads(resp.read().decode())
                feats = data.get("features", [])
                if feats:
                    coords = feats[0]["geometry"]["coordinates"]
                    lng, lat = float(coords[0]), float(coords[1])
                    st.cache_coords(addr, lat, lng)
                    done += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
            with _geocode_lock:
                _geocode_status["done"] = done
                _geocode_status["failed"] = failed
            _time.sleep(0.15)
    finally:
        st.close()
        with _geocode_lock:
            _geocode_status["running"] = False


@app.route("/map")
@login_required
def map_view() -> str:
    return render_template("map.html")


@app.route("/api/map")
@api_login_required
def api_map():
    """返回所有房源坐标。首次查询时自动 geocode 未缓存的地址。"""
    results: list[dict] = []
    need_geocode: list[dict] = []
    st = _storage()
    try:
        listings = st.get_map_listings()
        for l in listings:
            cached = st.get_cached_coords(l["address"])
            if cached:
                lat, lng = cached
                results.append({**l, "lat": lat, "lng": lng})
            else:
                need_geocode.append(l)
    finally:
        st.close()

    # Auto-geocode uncached addresses in background daemon thread,
    # so the API returns immediately without blocking.
    # 如果已有 geocode 任务在跑（手动或自动）则跳过，避免并发。
    if need_geocode:
        with _geocode_lock:
            if _geocode_status["running"]:
                need_geocode = []
            else:
                _geocode_status["running"] = True
                _geocode_status["total"] = len(need_geocode)
                _geocode_status["done"] = 0
                _geocode_status["failed"] = 0
    if need_geocode:
        addrs = [l["address"] for l in need_geocode]
        threading.Thread(target=_run_geocode_worker, args=(addrs,), daemon=True).start()

    return jsonify({"listings": results})


@app.route("/api/map/geocode", methods=["POST"])
@admin_api_required
@csrf_required
def api_map_geocode():
    """启动后台地理编码任务。进度通过 GET /api/map/geocode/status 查询。"""
    with _geocode_lock:
        if _geocode_status["running"]:
            s = dict(_geocode_status)
            return jsonify({"ok": True, "running": True, "total": s["total"], "done": s["done"], "failed": s["failed"]})

    st = _storage()
    try:
        listings = st.get_map_listings()
        uncached = [l for l in listings if not st.get_cached_coords(l["address"])]
    finally:
        st.close()

    if not uncached:
        return jsonify({"ok": True, "total": 0, "done": 0, "failed": 0, "running": False, "finished": True})

    with _geocode_lock:
        _geocode_status["running"] = True
        _geocode_status["total"] = len(uncached)
        _geocode_status["done"] = 0
        _geocode_status["failed"] = 0

    addrs = [l["address"] for l in uncached]
    threading.Thread(target=_run_geocode_worker, args=(addrs,), daemon=True).start()
    return jsonify({"ok": True, "running": True, "total": len(uncached), "done": 0, "failed": 0})


@app.route("/api/map/geocode/status")
@api_login_required
def api_map_geocode_status():
    """查询地理编码任务进度。"""
    with _geocode_lock:
        s = dict(_geocode_status)
    return jsonify({"running": s["running"], "total": s["total"], "done": s["done"], "failed": s["failed"],
                    "finished": not s["running"] and s["total"] > 0})


@app.route("/api/neighborhoods")
@api_login_required
def api_neighborhoods():
    """返回指定城市的所有片区（供用户过滤表单动态加载）。"""
    cities = request.args.get("cities", "").split(",")
    cities = [c.strip() for c in cities if c.strip()]
    st = _storage()
    try:
        hoods = st.get_feature_values("Neighborhood", cities=cities or None)
    except Exception:
        hoods = []
    finally:
        st.close()
    return jsonify({"neighborhoods": hoods})


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


@app.route("/system")
@admin_required
def system_info():
    import subprocess as _sp
    info: dict = {}

    # ── 进程 ──
    pid = _monitor_pid()
    info["monitor_running"] = pid is not None
    info["monitor_pid"] = pid
    info["web_pid"] = os.getpid()

    # ── 数据库 ──
    st = _storage()
    try:
        info["total_listings"] = st.count_all()
        info["last_scrape"] = st.get_meta("last_scrape_at")
        info["last_count"] = st.get_meta("last_scrape_count")
        info["unread_notifications"] = st.count_unread_notifications()
        info["total_changes"] = st._conn.execute("SELECT COUNT(*) FROM status_changes").fetchone()[0]
        info["total_notifications"] = st._conn.execute("SELECT COUNT(*) FROM web_notifications").fetchone()[0]
    finally:
        st.close()

    # ── 配置 ──
    from config import load_config as _lc
    # 强制从 .env 文件重新加载（override=True），因为 os.environ 可能仍是旧值
    from dotenv import load_dotenv as _ld
    _ld(dotenv_path=ENV_PATH, override=True)
    cfg = _lc()
    info["cities"] = [c.name for c in cfg.cities]
    info["check_interval"] = cfg.check_interval
    info["peak_interval"] = cfg.peak_interval
    info["peak_start"] = cfg.peak_start
    info["peak_end"] = cfg.peak_end
    info["min_interval"] = cfg.min_interval
    info["log_level"] = cfg.log_level

    # ── 用户 ──
    from users import load_users as _lu
    users = _lu()
    info["users_total"] = len(users)
    info["users_active"] = sum(1 for u in users if u.enabled)

    # ── 环境 ──
    info["python"] = sys.version
    info["platform"] = sys.platform
    info["base_dir"] = str(BASE_DIR)
    info["data_dir"] = str(DATA_DIR)

    # git
    try:
        r = _sp.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=str(BASE_DIR))
        info["git_hash"] = r.stdout.strip() if r.returncode == 0 else "—"
    except Exception:
        info["git_hash"] = "—"
    try:
        r = _sp.run(["git", "log", "-1", "--format=%ci"], capture_output=True, text=True, cwd=str(BASE_DIR))
        info["git_date"] = r.stdout.strip() if r.returncode == 0 else "—"
    except Exception:
        info["git_date"] = "—"

    return render_template("system.html", info=info)


# ------------------------------------------------------------------ #
# 日志查看
# ------------------------------------------------------------------ #

_LOG_PATH = DATA_DIR / "monitor.log"


@app.route("/api/logs")
@admin_api_required
def api_logs():
    try:
        lines_param = int(request.args.get("lines", 200))
    except (TypeError, ValueError):
        lines_param = 200
    lines_param = max(1, min(lines_param, 2000))

    if not _LOG_PATH.exists():
        return jsonify({"lines": [], "size": 0, "note": "no log file yet"})

    try:
        size = _LOG_PATH.stat().st_size
        with open(_LOG_PATH, encoding="utf-8") as f:
            all_lines = f.readlines()
        tail = all_lines[-lines_param:] if len(all_lines) > lines_param else all_lines
        return jsonify({"lines": [line.rstrip("\n") for line in tail], "size": size})
    except Exception as e:
        return jsonify({"lines": [], "size": 0, "error": str(e)}), 500


@app.route("/api/logs/clear", methods=["POST"])
@admin_api_required
@csrf_required
def api_logs_clear():
    try:
        if _LOG_PATH.exists():
            _LOG_PATH.write_text("", encoding="utf-8")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/logs")
@admin_required
def logs_view():
    return render_template("logs.html")


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

# 进程控制工具（_pid_exists / _monitor_pid / _write_reload_request +
# PID_FILE / RELOAD_REQUEST_FILE 常量）已迁移到 app.process_ctrl，
# 在文件顶部统一 import。


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


@app.route("/health")
def health():
    # 只检查 Web 进程是否存活（能响应 HTTP 即代表存活）。
    # monitor 运行状态通过 "monitor" 字段透出，供外部观测，
    # 但不影响 HTTP 状态码——管理员主动停止监控不应让容器变 unhealthy。
    monitor_ok = _monitor_pid() is not None
    return jsonify({"ok": True, "monitor": monitor_ok}), 200


@app.route("/api/reload", methods=["POST"])
@admin_api_required
@csrf_required
def api_reload():
    pid = _monitor_pid()
    if pid is None:
        return jsonify({"ok": False, "error": "监控程序未运行，请先启动监控"}), 400

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
# API — 监控进程启停
# ------------------------------------------------------------------ #

@app.route("/api/monitor/start", methods=["POST"])
@admin_api_required
@csrf_required
def api_monitor_start():
    """启动后台监控进程（monitor.py）。"""
    if _monitor_pid() is not None:
        return jsonify({"ok": False, "error": "监控已在运行"}), 409
    try:
        if getattr(sys, "frozen", False):
            subprocess.Popen(
                [sys.executable, "--run-monitor"],
                cwd=str(BASE_DIR),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            subprocess.Popen(
                [sys.executable, str(BASE_DIR / "monitor.py")],
                cwd=str(BASE_DIR),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        return jsonify({"ok": True, "message": "已启动"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/monitor/stop", methods=["POST"])
@admin_api_required
@csrf_required
def api_monitor_stop():
    """停止后台监控进程。"""
    pid = _monitor_pid()
    if pid is None:
        return jsonify({"ok": False, "error": "监控未在运行"}), 409
    try:
        os.kill(pid, signal.SIGTERM)
        return jsonify({"ok": True, "message": "已发送停止信号"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/shutdown", methods=["POST"])
@admin_api_required
@csrf_required
def api_shutdown():
    """关闭监控和 Web 面板。"""
    # 先停监控
    pid = _monitor_pid()
    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass

    # 延迟 300ms 后杀死 web 自身，保证响应能返回给前端
    def _delayed():
        import time as _t
        _t.sleep(0.3)
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=_delayed, daemon=True).start()
    return jsonify({"ok": True, "message": "正在关闭..."})


# ------------------------------------------------------------------ #
# API — 数据库重置
# ------------------------------------------------------------------ #

@app.route("/api/reset-db", methods=["POST"])
@admin_api_required
@csrf_required
def api_reset_db():
    """
    清空全部数据表（listings / status_changes / meta / web_notifications）。

    需在请求体中传 {"confirm": true} 作为二次确认。
    监控进程运行中也可执行——Storage 使用 WAL 模式，reset 事务与监控写入不冲突。
    """
    data = request.get_json(silent=True) or {}
    if not data.get("confirm"):
        return jsonify({"ok": False, "error": "缺少二次确认（confirm: true）"}), 400

    st = _storage()
    try:
        st.reset_all()
        return jsonify({"ok": True, "message": "数据库已清空（listings / status_changes / meta / 通知）"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        st.close()


# ------------------------------------------------------------------ #
# API — Web 通知
# ------------------------------------------------------------------ #

@app.route("/api/notifications")
@admin_api_required
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
@admin_api_required
@csrf_required
def api_notifications_read():
    """标记所有通知为已读（或指定 ids 数组）。"""
    data = request.get_json(silent=True) or {}
    ids  = data.get("ids")  # None → 全部；list[int] → 指定
    if ids is not None:
        if not isinstance(ids, list):
            return jsonify({"ok": False, "error": "ids 必须是数组"}), 400
        try:
            ids = [int(i) for i in ids]
        except (ValueError, TypeError):
            return jsonify({"ok": False, "error": "ids 元素必须是整数"}), 400

    storage = _storage()
    try:
        storage.mark_notifications_read(ids=ids)
    finally:
        storage.close()

    return jsonify({"ok": True})


@app.route("/api/events")
@admin_api_required
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
        st = _storage()
        try:
            while not stop.is_set() and _time.monotonic() < expires:
                rows = st.get_notifications_since(last_id)

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
            st.close()
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
