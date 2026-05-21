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
    finally:
        st.close()
    return {
        "running": pid is not None,
        "pid": pid,
        "last_scrape": last_scrape,
        "last_count": last_count,
    }


def get_web_status() -> dict[str, Any]:
    """Return legacy Web panel status payload."""
    pid = monitor_pid()
    users = load_users()
    return {
        "running": pid is not None,
        "pid": pid,
        "users": len(users),
        "active_users": sum(1 for u in users if u.enabled),
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

