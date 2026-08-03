"""
路由：系统状态 + 日志 + 健康检查 + 数据库重置 + 平台信息

挂载的 endpoint
- GET  /system          → system_info（页面）
- GET  /monitoring      → monitoring_view（页面：分 source 数据健康）
- GET  /api/monitoring  → api_monitoring
- GET  /logs            → logs_view（页面）
- GET  /api/logs        → api_logs（支持 q / level / since / until / scan 过滤）
- GET  /api/logs/files  → api_logs_files
- POST /api/logs/clear  → api_logs_clear
- POST /api/reset-db    → api_reset_db（二次确认 confirm:true）
- GET  /api/status      → api_status
- GET  /api/platform    → api_platform
- GET  /health          → health（无需鉴权）
"""
from __future__ import annotations

import os
import re
import subprocess as _sp
import sys
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from config import BASE_DIR, DATA_DIR, ENV_PATH
from users import load_users

from app.auth import admin_api_required, admin_required, api_login_required
from app.csrf import csrf_required
from app.db import storage
from app.process_ctrl import monitor_pid

# monitor 心跳容许的最大停滞秒数。默认 15 分钟 ≈ 3–4 个抓取轮次，既能容忍
# 管理员为部署 / 调试短暂停掉监控，也不会让一次被遗忘的暂停无限期潜伏。
_HEARTBEAT_MAX_AGE = int(os.environ.get("MONITOR_HEARTBEAT_MAX_AGE", "900"))
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


# ── 日志过滤 ─────────────────────────────────────────────────────────
#
# 在此之前 /api/logs 只能 tail，而且每次轮询都 readlines() 整个文件。想看
# "凌晨三点发生了什么" 只能 ssh 上去 grep——这正是可观测性方案要消掉的那件事。

_LOG_SCAN_MB_DEFAULT = 8.0
_LOG_SCAN_MB_MAX = 64.0

# 日志格式来自 monitor.py / web.py 的 "%(asctime)s [%(levelname)s] %(name)s: ..."
# asctime 默认形如 2026-08-03 10:42:15,123
_LOG_HEAD_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})[,.]?\d*\s+\[([A-Z]+)\]"
)


def _parse_log_time(raw: str | None) -> str | None:
    """把 since/until 归一成可与日志时间戳字典序比较的 'YYYY-MM-DD HH:MM:SS'。

    时间戳是零填充的定宽格式，所以字典序 == 时间序，不需要真的解析成 datetime。
    只补齐位数：'2026-08-03' → '2026-08-03 00:00:00'。
    """
    s = (raw or "").strip().replace("T", " ")
    if not s:
        return None
    if not re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return None
    if len(s) == 10:            # 只有日期
        return s + " 00:00:00"
    if len(s) == 13:            # 到小时
        return s + ":00:00"
    if len(s) == 16:            # 到分钟
        return s + ":00"
    return s[:19]


def _read_tail(path: Path, scan_bytes: int) -> tuple[str, int, bool]:
    """读文件尾部至多 scan_bytes，返回 (文本, 实际读取字节, 是否被截断)。

    替掉原来的 ``f.readlines()``——那会把整个日志读进内存，而前端是轮询调用的。
    """
    size = path.stat().st_size
    start = max(0, size - scan_bytes)
    with open(path, "rb") as f:
        f.seek(start)
        raw = f.read()
    text = raw.decode("utf-8", errors="replace")
    if start > 0:
        # 从中间切进去多半落在某行中央，丢掉这半行
        nl = text.find("\n")
        text = text[nl + 1:] if nl >= 0 else ""
    return text, len(raw), start > 0


def _group_records(text: str) -> list[dict]:
    """把日志行组装成"记录"：一条带时间戳的行 + 它后面所有无头部的续行。

    必须按记录而不是按行过滤。traceback 的续行既没有时间戳也没有级别，
    逐行过滤会把 traceback 拦腰截断——而 traceback 恰恰是最需要看的部分。
    """
    records: list[dict] = []
    for line in text.split("\n"):
        if not line:
            continue
        m = _LOG_HEAD_RE.match(line)
        if m:
            records.append({
                "ts": m.group(1).replace("T", " "),
                "level": m.group(2),
                "lines": [line],
            })
        elif records and records[-1]["ts"]:
            # 只有前一条**有头部**时才当续行并入。否则（整个文件都不是标准
            # 格式，或扫描窗口从半截切进来）就会把所有行并成一条巨型记录，
            # lines 上限彻底失效——tail 500 行会返回整个文件。
            records[-1]["lines"].append(line)
        else:
            # 孤儿行：不符合日志格式，或它的头部在扫描窗口之外。各自成条，
            # 保证退化成朴素 tail；没有时间戳和级别，level/时间过滤会滤掉它。
            records.append({"ts": "", "level": "", "lines": [line]})
    return records


def _record_matches(
    rec: dict, *, q: str, levels: set[str], since: str | None, until: str | None,
) -> bool:
    if levels and rec["level"] not in levels:
        return False
    ts = rec["ts"]
    if since and (not ts or ts < since):
        return False
    if until and (not ts or ts > until):
        return False
    if q:
        needle = q.lower()
        # 在整条记录里找，不是只在头部行——报错的关键字多半在 traceback 里
        if not any(needle in ln.lower() for ln in rec["lines"]):
            return False
    return True


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
    """日志尾部 + 过滤。

    过滤参数（都可省略，省略即退化为原来的纯 tail 行为）:
      q      关键字，大小写不敏感子串
      level  级别，逗号分隔（ERROR,WARNING）
      since  起始时间 'YYYY-MM-DD HH:MM[:SS]'（也接受 T 分隔）
      until  结束时间，同上
      scan   回溯扫描多少 MB，默认 8，上限 64
    """
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

    q = (request.args.get("q") or "").strip()
    levels = {
        s.strip().upper()
        for s in (request.args.get("level") or "").split(",")
        if s.strip()
    }
    since = _parse_log_time(request.args.get("since"))
    until = _parse_log_time(request.args.get("until"))
    try:
        scan_mb = float(request.args.get("scan", _LOG_SCAN_MB_DEFAULT))
    except (TypeError, ValueError):
        scan_mb = _LOG_SCAN_MB_DEFAULT
    scan_bytes = int(max(0.25, min(scan_mb, _LOG_SCAN_MB_MAX)) * 1024 * 1024)

    try:
        size = log_path.stat().st_size
        text, scanned, truncated = _read_tail(log_path, scan_bytes)
        records = _group_records(text)
        matched = [
            rec for rec in records
            if _record_matches(rec, q=q, levels=levels, since=since, until=until)
        ]
        selected = matched[-lines_param:] if len(matched) > lines_param else matched
        out: list[str] = []
        for rec in selected:
            out.extend(rec["lines"])
        return jsonify({
            "lines": out,
            "size": size,
            "file": file_key,
            # 供前端提示「只看了最后 N MB」——过滤命中 0 条时，用户需要知道
            # 到底是"没有这条日志"还是"没扫到那么远"。这两者天差地别。
            "records": len(records),
            "matched": len(matched),
            "returned": len(selected),
            "scanned_bytes": scanned,
            "truncated": truncated,
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


def _heartbeat_age_seconds() -> float | None:
    """monitor 心跳距今多少秒；从未写过返回 None。"""
    st = storage()
    try:
        raw = st.get_meta("monitor_heartbeat_at", default="")
    finally:
        st.close()
    if not raw:
        return None
    try:
        beat = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if beat.tzinfo is None:
        beat = beat.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - beat).total_seconds()


def health():
    """无需鉴权的容器健康检查：Web **和** monitor 都要正常才算 healthy。

    以前这里只看 Web 能否响应，monitor 状态仅作为字段透出、不影响状态码。
    代价是 2026-06-13 起 monitor 停了 7 周，容器全程报 healthy，21 个活跃用户
    没有任何通知，也没有任何告警——盲区正好盖住了唯一重要的进程。

    判据用**心跳新鲜度**，不是「进程是否存在」：

    - 进程还在但循环卡死时，PID 检查看不出来，心跳能。
    - H2S 熔断冷却最长 6 小时，期间没有成功抓取但循环仍在转、心跳照常刷新。
      若改用 last_scrape_at，正常退避会被误报成故障。

    保留原来那条顾虑的合理部分：管理员为部署 / 调试短暂停掉监控，在
    ``MONITOR_HEARTBEAT_MAX_AGE`` 之内不会翻红；只有停够久（默认 15 分钟，
    约 3–4 个轮次）才暴露出来。心跳尚未写过时（全新部署、首轮未跑完）退回
    进程存活判断，避免冷启动误杀。
    """
    monitor_ok = is_monitor_running()
    age = _heartbeat_age_seconds()

    if age is None:
        # 还没有心跳：新装或首轮未完成，只能看进程在不在
        healthy = monitor_ok
        reason = "monitor 未运行" if not healthy else ""
    else:
        healthy = age <= _HEARTBEAT_MAX_AGE
        reason = (
            f"monitor 心跳已停滞 {int(age)}s（上限 {_HEARTBEAT_MAX_AGE}s）"
            if not healthy else ""
        )

    payload = {
        "ok": healthy,
        "monitor": monitor_ok,
        "heartbeat_age": int(age) if age is not None else None,
        "heartbeat_max_age": _HEARTBEAT_MAX_AGE,
    }
    if reason:
        payload["reason"] = reason
    return jsonify(payload), (200 if healthy else 503)


# ── 数据健康（可观测性面板）─────────────────────────────────────────
#
# 与 /health 的分工见 mcore/health.py 的模块文档：/health 答"循环还活着吗"，
# 这里答"数据还对吗"。后者不参与容器健康判定——重启治不好解析器对不上。


@admin_required
def monitoring_view():
    # 时区交给前端做展示转换：round_at 存的是 UTC（与 last_scrape_at /
    # first_seen 一致），但页面必须显示成 TIMEZONE 本地时间——容器跑在
    # TZ=Europe/Amsterdam，日志的 asctime 就是那个时区。两边不一致的话，
    # 在 /logs 里按 14:00 过滤再回面板找对应轮次会差整整两小时。
    from config import TIMEZONE
    return render_template("monitoring.html", tz=TIMEZONE)


@admin_api_required
def api_monitoring():
    from mcore import health as _health
    from mcore import watchdog as _watchdog

    try:
        rounds_limit = int(request.args.get("rounds", 30))
    except (TypeError, ValueError):
        rounds_limit = 30
    rounds_limit = max(1, min(rounds_limit, 200))

    try:
        window = int(request.args.get("window", _health.DEFAULT_WINDOW))
    except (TypeError, ValueError):
        window = _health.DEFAULT_WINDOW
    window = max(1, min(window, 500))

    st = storage()
    try:
        report = _health.health_report(st, window=window)
        report["alerts"] = _watchdog.snapshot(st, window=window)
        report["rounds"] = st.recent_rounds_grouped(limit=rounds_limit)
        report["last_scrape_at"] = st.get_meta("last_scrape_at", default="")
        report["heartbeat_at"] = st.get_meta("monitor_heartbeat_at", default="")
    finally:
        st.close()
    report["monitor_running"] = is_monitor_running()
    # 所有时间戳都是 UTC ISO（canonical）；这个字段告诉调用方该按哪个时区展示。
    from config import TIMEZONE
    report["timezone"] = TIMEZONE
    return jsonify(report)


@admin_api_required
@csrf_required
def api_announcement():
    """给所有开启通知的用户群发一条公告。

    ``dry_run: true`` 只回报送达范围、不发送——群发不可撤回，发之前先看清楚
    会打扰到多少人。
    """
    from app.services.announcement_service import broadcast

    data = request.get_json(silent=True) or {}
    title = data.get("title") or ""
    body = data.get("body") or ""
    dry_run = bool(data.get("dry_run"))
    try:
        res = broadcast(title, body, dry_run=dry_run)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "dry_run": dry_run, **res.as_dict()})


def register(app: Flask) -> None:
    app.add_url_rule("/system",         endpoint="system_info",    view_func=system_info,    methods=["GET"])
    app.add_url_rule("/api/announcement", endpoint="api_announcement", view_func=api_announcement, methods=["POST"])
    app.add_url_rule("/monitoring",     endpoint="monitoring_view", view_func=monitoring_view, methods=["GET"])
    app.add_url_rule("/api/monitoring", endpoint="api_monitoring",  view_func=api_monitoring,  methods=["GET"])
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
