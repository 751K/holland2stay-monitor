"""
路由：系统状态 + 日志 + 健康检查 + 数据库重置 + 平台信息

挂载的 endpoint
- GET  /system          → system_info（页面）
- GET  /logs            → logs_view（页面）
- GET  /api/logs        → api_logs
- GET  /api/logs/files  → api_logs_files
- POST /api/logs/clear  → api_logs_clear
- POST /api/reset-db    → api_reset_db（二次确认 confirm:true）
- GET  /api/status      → api_status
- GET  /api/platform    → api_platform
- GET  /health          → health（无需鉴权）
"""
from __future__ import annotations

import os
import subprocess as _sp
import sys
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from config import BASE_DIR, DATA_DIR, ENV_PATH
from users import load_users

from app.auth import admin_api_required, admin_required, api_login_required
from app.csrf import csrf_required
from app.db import storage
from app.process_ctrl import monitor_pid

_LOG_PATH = DATA_DIR / "monitor.log"

# /api/logs?file=<key> 允许查看的日志文件白名单。
# 防止路径穿越（任意用户提交 file=../../etc/passwd 之类的 payload）。
_LOG_FILES: dict[str, Path] = {
    "monitor": DATA_DIR / "monitor.log",
    "errors":  DATA_DIR / "errors.log",
}


@admin_required
def system_info():
    info: dict = {}

    # ── 进程 ──
    pid = monitor_pid()
    info["monitor_running"] = pid is not None
    info["monitor_pid"] = pid
    info["web_pid"] = os.getpid()

    # ── 数据库 ──
    st = storage()
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
    users = load_users()
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


@admin_required
def logs_view():
    return render_template("logs.html")


@admin_api_required
def api_logs_files():
    """返回可用日志文件列表及各自大小，供前端渲染文件切换 tab。"""
    files = []
    for key, path in _LOG_FILES.items():
        try:
            size = path.stat().st_size if path.exists() else 0
        except OSError:
            size = 0
        files.append({"key": key, "size": size, "exists": path.exists()})
    return jsonify({"files": files})


@admin_api_required
def api_logs():
    try:
        lines_param = int(request.args.get("lines", 200))
    except (TypeError, ValueError):
        lines_param = 200
    lines_param = max(1, min(lines_param, 2000))

    # file= 参数走白名单（防路径穿越），默认 monitor
    file_key = request.args.get("file", "monitor")
    log_path = _LOG_FILES.get(file_key)
    if log_path is None:
        return jsonify({
            "lines": [], "size": 0,
            "error": f"unknown log file: {file_key!r}, allowed: {list(_LOG_FILES)}",
        }), 400

    if not log_path.exists():
        return jsonify({
            "lines": [], "size": 0,
            "note": f"{file_key} log file not yet created",
        })

    try:
        size = log_path.stat().st_size
        with open(log_path, encoding="utf-8") as f:
            all_lines = f.readlines()
        tail = all_lines[-lines_param:] if len(all_lines) > lines_param else all_lines
        return jsonify({
            "lines": [line.rstrip("\n") for line in tail],
            "size": size,
            "file": file_key,
        })
    except Exception as e:
        return jsonify({"lines": [], "size": 0, "error": str(e)}), 500


@admin_api_required
@csrf_required
def api_logs_clear():
    """清空指定日志（file=monitor|errors，默认 monitor）。"""
    file_key = request.args.get("file", "monitor")
    log_path = _LOG_FILES.get(file_key)
    if log_path is None:
        return jsonify({"ok": False, "error": f"unknown log file: {file_key!r}"}), 400
    try:
        if log_path.exists():
            log_path.write_text("", encoding="utf-8")
        return jsonify({"ok": True, "file": file_key})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


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

    st = storage()
    try:
        st.reset_all()
        return jsonify({"ok": True, "message": "数据库已清空（listings / status_changes / meta / 通知）"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        st.close()


@api_login_required
def api_status():
    pid = monitor_pid()
    users = load_users()
    return jsonify({
        "running": pid is not None,
        "pid": pid,
        "users": len(users),
        "active_users": sum(1 for u in users if u.enabled),
    })


@api_login_required
def api_platform():
    """返回服务器平台信息，用于面板判断 iMessage 是否可用。"""
    return jsonify({"macos": sys.platform == "darwin", "platform": sys.platform})


def health():
    """无需鉴权：只检查 Web 进程是否存活（能响应 HTTP 即代表存活）。
    monitor 运行状态通过 "monitor" 字段透出，供外部观测，
    但不影响 HTTP 状态码——管理员主动停止监控不应让容器变 unhealthy。"""
    monitor_ok = monitor_pid() is not None
    return jsonify({"ok": True, "monitor": monitor_ok}), 200


def register(app: Flask) -> None:
    app.add_url_rule("/system",         endpoint="system_info",    view_func=system_info,    methods=["GET"])
    app.add_url_rule("/logs",           endpoint="logs_view",      view_func=logs_view,      methods=["GET"])
    app.add_url_rule("/api/logs/files", endpoint="api_logs_files", view_func=api_logs_files, methods=["GET"])
    app.add_url_rule("/api/logs",       endpoint="api_logs",       view_func=api_logs,       methods=["GET"])
    app.add_url_rule("/api/logs/clear", endpoint="api_logs_clear", view_func=api_logs_clear, methods=["POST"])
    app.add_url_rule("/api/reset-db",   endpoint="api_reset_db",   view_func=api_reset_db,   methods=["POST"])
    app.add_url_rule("/api/status",     endpoint="api_status",     view_func=api_status,     methods=["GET"])
    app.add_url_rule("/api/platform",   endpoint="api_platform",   view_func=api_platform,   methods=["GET"])
    app.add_url_rule("/health",         endpoint="health",         view_func=health,         methods=["GET"])
