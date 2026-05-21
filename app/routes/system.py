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
from app.services.monitor_service import get_web_status, is_monitor_running

_LOG_PATH = DATA_DIR / "monitor.log"

# /api/logs?file=<key> 允许查看的日志文件白名单。
# 防止路径穿越（任意用户提交 file=../../etc/passwd 之类的 payload）。
_LOG_FILES: dict[str, Path] = {
    "monitor": DATA_DIR / "monitor.log",
    "errors":  DATA_DIR / "errors.log",
    "web":     DATA_DIR / "web.log",
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
        info["total_changes"] = st.conn.execute("SELECT COUNT(*) FROM status_changes").fetchone()[0]
        info["total_notifications"] = st.conn.execute("SELECT COUNT(*) FROM web_notifications").fetchone()[0]
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


# ── 崩溃报告页 ───────────────────────────────────────────────────────
#
# 数据来源：data/crash_reports/*.json （由 /api/v1/diagnostics/crash 落盘）
# 文件名形如 20260520T0030Z-crash-abc12345.json
#
# 安全：admin only；filename 来自目录扫描而不是用户输入，但 view 端点
# 仍做 basename 白名单防御（拒绝含 / 或 .. 的 id）。


_CRASH_DIR = DATA_DIR / "crash_reports"


def _read_crash_summaries(limit: int = 200) -> list[dict]:
    """扫目录返回最近 N 份 crash 报告的元信息（不含 payload，节省内存）。"""
    import json as _json
    if not _CRASH_DIR.exists():
        return []
    items: list[dict] = []
    files = sorted(
        _CRASH_DIR.glob("*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:limit]
    for f in files:
        try:
            data = _json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        items.append({
            "id": f.name,
            "size": f.stat().st_size,
            "received_at": data.get("received_at", ""),
            "kind": data.get("kind", "?"),
            "role": data.get("role", "?"),
            "user_id": data.get("user_id", ""),
            "app_version": data.get("app_version", ""),
            "ios_version": data.get("ios_version", ""),
            "device_model": data.get("device_model", ""),
        })
    return items


def _safe_crash_path(crash_id: str) -> Path | None:
    """白名单校验后返回报告文件路径，非法 id 返 None。"""
    # 只允许我们自己写出来的命名格式：数字 / 字母 / `-` / `.json`
    if (
        not crash_id
        or "/" in crash_id
        or "\\" in crash_id
        or ".." in crash_id
        or not crash_id.endswith(".json")
    ):
        return None
    path = _CRASH_DIR / crash_id
    try:
        # resolve 之后必须仍在 _CRASH_DIR 之下（防止 symlink 逃逸）
        resolved = path.resolve(strict=False)
        if not str(resolved).startswith(str(_CRASH_DIR.resolve())):
            return None
    except Exception:
        return None
    if not path.is_file():
        return None
    return path


@admin_required
def crashes_view():
    """崩溃报告列表页。"""
    return render_template(
        "crashes.html",
        crashes=_read_crash_summaries(),
        crash_dir=str(_CRASH_DIR),
    )


@admin_api_required
def api_crash_detail(crash_id: str):
    """返回单份崩溃报告完整 JSON。"""
    import json as _json
    path = _safe_crash_path(crash_id)
    if path is None:
        return jsonify({"ok": False, "error": "not found"}), 404
    try:
        data = _json.loads(path.read_text(encoding="utf-8"))
        return jsonify({"ok": True, "data": data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@admin_api_required
@csrf_required
def api_crash_delete(crash_id: str):
    """物理删除单份崩溃报告。"""
    path = _safe_crash_path(crash_id)
    if path is None:
        return jsonify({"ok": False, "error": "not found"}), 404
    try:
        path.unlink()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@admin_api_required
@csrf_required
def api_crashes_clear():
    """批量删除：根据 body.ids 列表删除（前端勾选后调用）。"""
    body = request.get_json(silent=True) or {}
    ids = body.get("ids") or []
    if not isinstance(ids, list):
        return jsonify({"ok": False, "error": "ids must be list"}), 400
    deleted = 0
    for cid in ids:
        if not isinstance(cid, str):
            continue
        path = _safe_crash_path(cid)
        if path is not None:
            try:
                path.unlink()
                deleted += 1
            except Exception:
                pass
    return jsonify({"ok": True, "deleted": deleted})


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
    return jsonify(get_web_status())


@api_login_required
def api_platform():
    """返回服务器平台信息，用于面板判断 iMessage 是否可用。"""
    return jsonify({"macos": sys.platform == "darwin", "platform": sys.platform})


def health():
    """无需鉴权：只检查 Web 进程是否存活（能响应 HTTP 即代表存活）。
    monitor 运行状态通过 "monitor" 字段透出，供外部观测，
    但不影响 HTTP 状态码——管理员主动停止监控不应让容器变 unhealthy。"""
    monitor_ok = is_monitor_running()
    return jsonify({"ok": True, "monitor": monitor_ok}), 200


def register(app: Flask) -> None:
    app.add_url_rule("/system",         endpoint="system_info",    view_func=system_info,    methods=["GET"])
    app.add_url_rule("/logs",           endpoint="logs_view",      view_func=logs_view,      methods=["GET"])
    app.add_url_rule("/crashes",        endpoint="crashes_view",   view_func=crashes_view,   methods=["GET"])
    app.add_url_rule("/api/logs/files", endpoint="api_logs_files", view_func=api_logs_files, methods=["GET"])
    app.add_url_rule("/api/logs",       endpoint="api_logs",       view_func=api_logs,       methods=["GET"])
    app.add_url_rule("/api/logs/clear", endpoint="api_logs_clear", view_func=api_logs_clear, methods=["POST"])
    app.add_url_rule("/api/reset-db",   endpoint="api_reset_db",   view_func=api_reset_db,   methods=["POST"])
    app.add_url_rule("/api/status",     endpoint="api_status",     view_func=api_status,     methods=["GET"])
    app.add_url_rule("/api/platform",   endpoint="api_platform",   view_func=api_platform,   methods=["GET"])
    app.add_url_rule("/health",         endpoint="health",         view_func=health,         methods=["GET"])
    app.add_url_rule("/api/crashes/<crash_id>",        endpoint="api_crash_detail",  view_func=api_crash_detail,  methods=["GET"])
    app.add_url_rule("/api/crashes/<crash_id>/delete", endpoint="api_crash_delete",  view_func=api_crash_delete,  methods=["POST"])
    app.add_url_rule("/api/crashes/clear",             endpoint="api_crashes_clear", view_func=api_crashes_clear, methods=["POST"])
