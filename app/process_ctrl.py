"""
监控进程控制工具：跨平台 PID 探测 + 文件触发的热重载请求
================================================================

依赖
----
- 仅标准库
- config.DATA_DIR（确定 PID 文件 / reload 请求文件路径）

设计
----
本模块完全无 Flask 依赖，可以被 monitor.py 或独立 CLI 工具复用，
不强制走 Web 上下文。Web 路由层只负责调用 + 把结果包成 JSON。
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from config import DATA_DIR

PID_FILE: Path            = DATA_DIR / "monitor.pid"
RELOAD_REQUEST_FILE: Path = DATA_DIR / "monitor.reload"


def pid_exists(pid: int) -> bool:
    """
    跨平台检查 PID 是否仍然存活。

    - POSIX: 使用 `os.kill(pid, 0)`
    - Windows: 使用 Win32 `OpenProcess + GetExitCodeProcess`

    Windows 上 `os.kill(pid, 0)` 并不可靠，某些场景会抛出
    `OSError: [WinError 11]`，因此需要单独走 WinAPI。
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
        kernel32.OpenProcess.restype  = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype  = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype  = wintypes.BOOL

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


def monitor_pid() -> int | None:
    """读 PID 文件并验证进程仍存活；不存在或已死返回 None。"""
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
        return pid if pid_exists(pid) else None
    except ValueError:
        return None


def write_reload_request() -> None:
    """写入文件触发的热重载请求，供 Windows 和信号失败场景使用。"""
    RELOAD_REQUEST_FILE.parent.mkdir(parents=True, exist_ok=True)
    RELOAD_REQUEST_FILE.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
