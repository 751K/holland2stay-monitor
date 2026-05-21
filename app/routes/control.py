"""
路由：监控进程控制 + 配置热重载 + 整体关闭

挂载的 endpoint
- POST /api/reload         → api_reload
- POST /api/monitor/start  → api_monitor_start
- POST /api/monitor/stop   → api_monitor_stop
- POST /api/shutdown       → api_shutdown
"""
from __future__ import annotations

import os
import threading

from flask import Flask, jsonify

from app.auth import admin_api_required
from app.csrf import csrf_required
from app.services.monitor_service import (
    MonitorServiceError,
    is_monitor_running,
    reload_monitor,
    start_monitor,
    stop_monitor,
    terminate_process,
)


def _terminate(pid: int) -> None:
    """Backward-compatible alias for shutdown tests and older imports."""
    terminate_process(pid)


@admin_api_required
@csrf_required
def api_reload():
    try:
        result = reload_monitor()
    except MonitorServiceError as e:
        msg = "监控程序未运行，请先启动监控" if e.status == 400 else str(e)
        return jsonify({"ok": False, "error": msg}), e.status
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    if result.get("fallback"):
        message = "信号发送失败，已回退为文件触发重载"
    elif result.get("method") == "file":
        message = "已写入重载请求，配置将在 1 秒内检测并生效"
    else:
        message = "重载信号已发送，配置将在本轮抓取结束后生效"
    return jsonify({"ok": True, "message": message})


@admin_api_required
@csrf_required
def api_monitor_start():
    """启动后台监控进程（monitor.py）。"""
    try:
        result = start_monitor()
    except MonitorServiceError as e:
        return jsonify({"ok": False, "error": str(e)}), e.status
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    payload = {"ok": True, "message": "已启动"}
    if "method" in result:
        payload["method"] = result["method"]
    return jsonify(payload)


@admin_api_required
@csrf_required
def api_monitor_stop():
    """停止后台监控进程。"""
    try:
        result = stop_monitor()
    except MonitorServiceError as e:
        return jsonify({"ok": False, "error": str(e)}), e.status
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    if result.get("method") == "supervisor":
        return jsonify({"ok": True, "message": "已停止", "method": "supervisor"})
    return jsonify({"ok": True, "message": "已发送停止信号"})


@admin_api_required
@csrf_required
def api_shutdown():
    """关闭监控和 Web 面板。"""
    # 先停监控
    if is_monitor_running():
        try:
            stop_monitor()
        except Exception:
            pass

    # 延迟 300ms 后杀死 web 自身，保证响应能返回给前端
    def _delayed():
        import time as _t
        _t.sleep(0.3)
        _terminate(os.getpid())

    threading.Thread(target=_delayed, daemon=True).start()
    return jsonify({"ok": True, "message": "正在关闭..."})


def register(app: Flask) -> None:
    app.add_url_rule("/api/reload",        endpoint="api_reload",        view_func=api_reload,        methods=["POST"])
    app.add_url_rule("/api/monitor/start", endpoint="api_monitor_start", view_func=api_monitor_start, methods=["POST"])
    app.add_url_rule("/api/monitor/stop",  endpoint="api_monitor_stop",  view_func=api_monitor_stop,  methods=["POST"])
    app.add_url_rule("/api/shutdown",      endpoint="api_shutdown",      view_func=api_shutdown,      methods=["POST"])
