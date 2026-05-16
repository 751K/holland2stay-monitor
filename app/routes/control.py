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
import signal
import subprocess
import sys
import threading

from flask import Flask, jsonify

from config import BASE_DIR

from app.auth import admin_api_required
from app.csrf import csrf_required
from app.process_ctrl import (
    monitor_pid,
    supervisorctl_available,
    supervisorctl_monitor,
    write_reload_request,
)


def _terminate(pid: int) -> None:
    """跨平台终止进程：POSIX 用 SIGTERM，Windows 用 terminate()。"""
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(1, False, pid)  # PROCESS_TERMINATE = 1
        if handle:
            kernel32.TerminateProcess(handle, 0)
            kernel32.CloseHandle(handle)
    else:
        os.kill(pid, signal.SIGTERM)


@admin_api_required
@csrf_required
def api_reload():
    pid = monitor_pid()
    if pid is None:
        return jsonify({"ok": False, "error": "监控程序未运行，请先启动监控"}), 400

    # Windows 没有可靠的 SIGHUP 语义，统一改为写入 reload 请求文件。
    # 监控进程会在等待间隙轮询该文件并提前热重载。
    if os.name == "nt" or not hasattr(signal, "SIGHUP"):
        try:
            write_reload_request()
            return jsonify({"ok": True, "message": "已写入重载请求，配置将在 1 秒内检测并生效"})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    try:
        os.kill(pid, signal.SIGHUP)
        return jsonify({"ok": True, "message": "重载信号已发送，配置将在本轮抓取结束后生效"})
    except Exception:
        # 回退到文件触发机制，避免因信号发送失败导致 Web 面板无法应用配置。
        try:
            write_reload_request()
            return jsonify({"ok": True, "message": "信号发送失败，已回退为文件触发重载"})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500


@admin_api_required
@csrf_required
def api_monitor_start():
    """启动后台监控进程（monitor.py）。"""
    if monitor_pid() is not None:
        return jsonify({"ok": False, "error": "监控已在运行"}), 409
    try:
        if supervisorctl_available():
            r = supervisorctl_monitor("start")
            if r.returncode != 0:
                return jsonify({"ok": False, "error": (r.stderr or r.stdout or "supervisorctl start failed").strip()}), 500
            return jsonify({"ok": True, "message": "已启动", "method": "supervisor"})
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


@admin_api_required
@csrf_required
def api_monitor_stop():
    """停止后台监控进程。"""
    pid = monitor_pid()
    if pid is None:
        return jsonify({"ok": False, "error": "监控未在运行"}), 409
    try:
        if supervisorctl_available():
            r = supervisorctl_monitor("stop")
            if r.returncode != 0:
                return jsonify({"ok": False, "error": (r.stderr or r.stdout or "supervisorctl stop failed").strip()}), 500
            return jsonify({"ok": True, "message": "已停止", "method": "supervisor"})
        _terminate(pid)
        return jsonify({"ok": True, "message": "已发送停止信号"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@admin_api_required
@csrf_required
def api_shutdown():
    """关闭监控和 Web 面板。"""
    # 先停监控
    pid = monitor_pid()
    if pid is not None:
        try:
            _terminate(pid)
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
