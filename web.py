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
import uuid
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any

from dotenv import dotenv_values, set_key
from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for

sys.path.insert(0, str(Path(__file__).parent))
from config import KNOWN_CITIES, AutoBookConfig, ListingFilter  # noqa: E402
from storage import Storage                                      # noqa: E402
from users import UserConfig, get_user, load_users, save_users  # noqa: E402

# ------------------------------------------------------------------ #
# 常量
# ------------------------------------------------------------------ #

BASE_DIR = Path(__file__).parent
ENV_PATH = BASE_DIR / ".env"
DB_PATH  = Path(os.environ.get("DB_PATH", "data/listings.db"))
PID_FILE = BASE_DIR / "data/monitor.pid"

# 全局配置可写入的 .env 键（通知/过滤/预订已移至 users.json）
_SETTINGS_KEYS = [
    "CHECK_INTERVAL", "LOG_LEVEL",
    # 智能轮询
    "PEAK_INTERVAL", "PEAK_START", "PEAK_END", "PEAK_WEEKDAYS_ONLY",
]

# ------------------------------------------------------------------ #
# Flask app
# ------------------------------------------------------------------ #

app = Flask(__name__, template_folder="templates")

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
    key_map = {
        "Type": "type", "Area": "area", "Occupancy": "occupancy",
        "Floor": "floor", "Finishing": "furnishing", "Energy": "energy_label",
        "Neighborhood": "neighborhood", "Building": "building",
    }
    result: dict[str, str] = {}
    for feat in items:
        if ": " in feat:
            raw_key, val = feat.split(": ", 1)
            result[key_map.get(raw_key, raw_key.lower())] = val
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
            next_url = request.form.get("next") or url_for("index")
            return redirect(next_url)

        flash("用户名或密码错误", "danger")

    return render_template("login.html", next=request.args.get("next", ""),
                           auth_enabled=_auth_enabled())


@app.route("/logout", methods=["POST"])
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

def _user_from_form(form, user_id: str | None = None) -> UserConfig:
    """从表单数据构建 UserConfig。"""
    def _fv(key: str):
        v = form.get(key, "").strip()
        return float(v) if v else None

    def _iv(key: str):
        v = form.get(key, "").strip()
        return int(v) if v else None

    def _lv(key: str) -> list[str]:
        v = form.get(key, "").strip()
        return [x.strip() for x in v.split(",") if x.strip()] if v else []

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
    ab = AutoBookConfig(
        enabled=form.get("AUTO_BOOK_ENABLED") == "true",
        dry_run=form.get("AUTO_BOOK_DRY_RUN", "true") != "false",
        email=form.get("AUTO_BOOK_EMAIL", ""),
        password=form.get("AUTO_BOOK_PASSWORD", ""),
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
        twilio_sid=form.get("TWILIO_ACCOUNT_SID", ""),
        twilio_token=form.get("TWILIO_AUTH_TOKEN", ""),
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
def user_edit(user_id: str) -> Any:
    users = load_users()
    user = get_user(users, user_id)
    if user is None:
        flash("用户不存在", "danger")
        return redirect(url_for("users_list"))

    if request.method == "POST":
        updated = _user_from_form(request.form, user_id=user_id)
        users = [updated if u.id == user_id else u for u in users]
        save_users(users)
        flash(f"✅ 用户「{updated.name}」已保存", "success")
        return redirect(url_for("user_edit", user_id=user_id))

    return render_template("user_form.html", user=user,
                           action=url_for("user_edit", user_id=user_id),
                           title=f"编辑用户 · {user.name}")


@app.route("/users/<user_id>/delete", methods=["POST"])
@login_required
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
def user_test_notify(user_id: str) -> Any:
    """逐渠道发送一条测试消息，返回每个渠道的成功/失败详情。"""
    import asyncio
    from datetime import datetime as _dt
    from notifier import IMessageNotifier, TelegramNotifier, WhatsAppNotifier

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
        rows = storage._conn.execute(
            """SELECT id, name, status, price_raw, available_from, url, city, features
               FROM listings
               WHERE available_from IS NOT NULL AND available_from != ''
               ORDER BY available_from"""
        ).fetchall()
    finally:
        storage.close()

    listings = []
    for r in rows:
        listings.append({
            "id":             r["id"],
            "name":           r["name"],
            "status":         r["status"],
            "price_raw":      r["price_raw"] or "",
            "available_from": r["available_from"],
            "url":            r["url"] or "",
            "city":           r["city"] or "",
        })
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
    days = int(request.args.get("days", 30))
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

def _monitor_pid() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        return None


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
def api_reload():
    pid = _monitor_pid()
    if pid is None:
        return jsonify({"ok": False, "error": "监控程序未运行，请先启动 monitor.py"}), 400
    try:
        os.kill(pid, signal.SIGHUP)
        return jsonify({"ok": True, "message": "重载信号已发送，配置将在本轮抓取结束后生效"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ------------------------------------------------------------------ #
# 入口
# ------------------------------------------------------------------ #

def main() -> None:
    parser = argparse.ArgumentParser(description="Holland2Stay Web 面板")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    print(f"Web 面板运行中 → http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
