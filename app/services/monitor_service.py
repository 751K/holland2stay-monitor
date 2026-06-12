"""
Shared monitor process service.

Routes should keep HTTP/auth/response formatting here:
- Web routes return legacy ``{"ok": ..., "message": ...}`` JSON.
- API v1 routes return the standardized ``api_errors`` envelope.

This service owns the actual monitor process decisions so Web and mobile do not
drift when start/stop/reload behavior changes.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

from app.db import storage
from app.process_ctrl import (
    PID_FILE,
    monitor_pid,
    supervisorctl_available,
    supervisorctl_monitor,
    write_reload_request,
)
from config import BASE_DIR
from users import load_users


@dataclass(slots=True)
class MonitorServiceError(Exception):
    """Expected monitor operation failure that routes can map to HTTP."""

    message: str
    status: int = 500
    code: str = "server_error"

    def __str__(self) -> str:
        return self.message


def terminate_process(pid: int) -> None:
    """Terminate a process cross-platform."""
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(1, False, pid)  # PROCESS_TERMINATE = 1
        if handle:
            kernel32.TerminateProcess(handle, 0)
            kernel32.CloseHandle(handle)
        return
    os.kill(pid, signal.SIGTERM)


def get_monitor_status() -> dict[str, Any]:
    """Return monitor runtime status for API v1 admin clients."""
    pid = monitor_pid()
    st = storage()
    try:
        last_scrape = st.get_meta("last_scrape_at", default="")
        last_count = st.get_meta("last_scrape_count", default="")
        maintenance_seen = st.get_meta("upstream_maintenance_seen_at", default="")
    finally:
        st.close()
    return {
        "running": pid is not None,
        "pid": pid,
        "last_scrape": last_scrape,
        "last_count": last_count,
        # 非空 = H2S 正在维护；前端显示一个温和 banner
        "upstream_maintenance_since": maintenance_seen,
    }


def get_upstream_maintenance() -> dict[str, str]:
    """
    Banner-friendly snapshot of upstream maintenance state.

    Returns
    -------
    {"active": "1"/"", "since": "<ISO>", "last_seen": "<ISO>"}
    """
    st = storage()
    try:
        since = st.get_meta("upstream_maintenance_seen_at", default="")
        last = st.get_meta("upstream_maintenance_last_at", default="")
    finally:
        st.close()
    return {
        "active": "1" if since else "",
        "since": since,
        "last_seen": last,
    }


def get_web_status() -> dict[str, Any]:
    """Return legacy Web panel status payload."""
    pid = monitor_pid()
    users = load_users()
    running = pid is not None
    return {
        "running": running,
        "paused": not running,
        "pid": pid,
        "users": len(users),
        "active_users": sum(1 for u in users if u.enabled),
        "status": "running" if running else "paused",
        "status_label": "Monitor running" if running else "System paused",
        "status_message": (
            "Monitoring is active."
            if running
            else "Monitoring is paused. New listings, status changes, and auto-booking are not running until an admin starts the monitor."
        ),
    }


def is_monitor_running() -> bool:
    """Lightweight health helper."""
    return monitor_pid() is not None


def start_monitor() -> dict[str, Any]:
    """Start monitor.py or the supervisor-managed monitor program."""
    if monitor_pid() is not None:
        raise MonitorServiceError("监控已在运行", status=409, code="conflict")

    if supervisorctl_available():
        r = supervisorctl_monitor("start")
        if r.returncode != 0:
            msg = (r.stderr or r.stdout or "supervisorctl start failed").strip()
            raise MonitorServiceError(msg, status=500)
        return {"started": True, "method": "supervisor"}

    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--run-monitor"]
    else:
        cmd = [sys.executable, str(BASE_DIR / "monitor.py")]
    subprocess.Popen(
        cmd,
        cwd=str(BASE_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return {"started": True}


def stop_monitor() -> dict[str, Any]:
    """Stop the monitor process without triggering Docker supervisor autorestart."""
    pid = monitor_pid()
    if pid is None:
        raise MonitorServiceError("监控未在运行", status=409, code="conflict")

    if supervisorctl_available():
        r = supervisorctl_monitor("stop")
        if r.returncode != 0:
            msg = (r.stderr or r.stdout or "supervisorctl stop failed").strip()
            raise MonitorServiceError(msg, status=500)
        return {"stopped": True, "pid": pid, "method": "supervisor"}

    terminate_process(pid)
    PID_FILE.unlink(missing_ok=True)
    return {"stopped": True, "pid": pid}


def reload_monitor() -> dict[str, Any]:
    """Trigger monitor hot reload through SIGHUP or the file fallback."""
    pid = monitor_pid()
    if pid is None:
        raise MonitorServiceError("监控未在运行", status=400, code="validation")

    if os.name == "nt" or not hasattr(signal, "SIGHUP"):
        write_reload_request()
        return {"reload": True, "method": "file"}

    try:
        os.kill(pid, signal.SIGHUP)
        return {"reload": True, "method": "signal"}
    except Exception:
        write_reload_request()
        return {"reload": True, "method": "file", "fallback": True}


def restart_monitor() -> dict[str, Any]:
    """Full process restart: stop + wait + start.

    Reload（SIGHUP）只重读 .env / SQLite 配置，不重新 import Python 模块。
    改了 scraper / notifier / 业务逻辑代码时需要走 restart，让新进程从头加载。

    流程
    ----
    1. stop_monitor()  →  发 SIGTERM 让旧进程优雅退出（supervisor 接管时走 supervisorctl stop）
    2. 等 PID 文件消失（最多 5s）—— 老进程清掉 PID 文件后再启动，避免 monitor_pid() 误判为已运行
    3. start_monitor() →  起新进程

    supervisor 环境下两步都走 supervisorctl，autorestart 配置仍生效。
    """
    import time

    was_running = monitor_pid() is not None
    if was_running:
        # 让 stop_monitor 决定路径（直接 SIGTERM / supervisorctl stop）
        try:
            stop_monitor()
        except MonitorServiceError as e:
            # 已经停了 = 没问题，继续 start；其他错误上抛
            if e.status != 409:
                raise

        # 等老进程退出（PID 文件被 monitor.py finally 块清掉）。
        # 最多等 5s——supervisorctl stop 可能阻塞直到子进程死，
        # 但直接 SIGTERM 是异步的，poll PID 才知道真死。
        for _ in range(50):
            if monitor_pid() is None:
                break
            time.sleep(0.1)
        else:
            raise MonitorServiceError(
                "旧进程未在 5 秒内退出，可能正在收尾长操作；请稍后再试或先 Stop 再 Start",
                status=500,
            )

    result = start_monitor()
    return {
        "restarted": True,
        "was_running": was_running,
        **{k: v for k, v in result.items() if k != "started"},
    }
