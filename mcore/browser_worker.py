"""Managed, per-source subprocess for browser-backed scrapes.

The worker owns its Playwright thread and scraper cache. The parent is the
authority for proxy selection; requests for a proxy are sent back over the
private pipe so cooldown and rotating-session state stay in one process.
"""
from __future__ import annotations

import logging
import multiprocessing
import os
import signal
import subprocess
import time
import threading
from multiprocessing.connection import Connection

from scrapers.base import (
    BlockedError, OperationNotAllowedError, ProxyError, RateLimitError,
    ScrapeNetworkError, UpstreamMaintenanceError,
)

logger = logging.getLogger(__name__)
_ERROR_TYPES = {cls.__name__: cls for cls in (
    BlockedError, OperationNotAllowedError, ProxyError, RateLimitError,
    ScrapeNetworkError, UpstreamMaintenanceError,
)}


def _exception_chain(exc: BaseException) -> list[tuple[str, str]]:
    chain = []
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen and len(chain) < 20:
        seen.add(id(exc))
        chain.append((type(exc).__name__, str(exc)))
        exc = exc.__cause__ or exc.__context__
    return chain


def _restore_exception(chain: list[tuple[str, str]]) -> Exception:
    cause = None
    for name, message in reversed(chain):
        cls = _ERROR_TYPES.get(name, RuntimeError)
        exc = cls(message)
        exc.__cause__ = cause
        cause = exc
    return cause or ScrapeNetworkError("浏览器进程返回了空错误")


class _PipeLogHandler(logging.Handler):
    def __init__(self, conn: Connection, send_lock: threading.Lock):
        super().__init__()
        self.conn = conn
        self.send_lock = send_lock

    def emit(self, record):
        try:
            with self.send_lock:
                self.conn.send(("log", record.name, record.levelno, self.format(record)))
        except (BrokenPipeError, EOFError, OSError):
            pass


def _serve(conn: Connection, source: str) -> None:
    """Child entry point. Never use Playwright objects across process boundaries."""
    if os.name == "posix":
        os.setsid()

    send_lock = threading.Lock()
    handler = _PipeLogHandler(conn, send_lock)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    import config
    import scrapers
    import scrapers.holland2stay as h2s
    import scrapers.xior as xior

    def proxy_from_parent(source_name="", *, rotating=False):
        with send_lock:
            conn.send(("proxy", source_name, rotating))
        kind, value = conn.recv()
        if kind == "proxy_error":
            raise ScrapeNetworkError(f"主进程代理选择失败: {value}")
        if kind != "proxy_result":
            raise RuntimeError("代理响应协议错误")
        return value

    config.get_proxy_url = proxy_from_parent
    xior.get_proxy_url = proxy_from_parent
    with send_lock:
        conn.send(("ready",))
    while True:
        try:
            request = conn.recv()
        except EOFError:
            break
        if request[0] == "stop":
            break
        _, tasks, multi_source, env, detail_cache = request
        os.environ.clear()
        os.environ.update(env)
        from settings_store import _round_snapshot, snapshot_from_environment
        snapshot = snapshot_from_environment(env)
        _round_snapshot.set(snapshot)
        logger.info("浏览器工作进程 %s 配置版本: %s", source, snapshot.version)
        if source == "holland2stay":
            h2s.prime_detail_cache(detail_cache)
        try:
            result = scrapers.dispatch_scrape_tasks(tasks, multi_source=multi_source)
            response = ("result", result, h2s._DETAIL_CACHE if source == "holland2stay" else None)
        except Exception as exc:
            response = ("error", _exception_chain(exc), h2s._DETAIL_CACHE if source == "holland2stay" else None)
        try:
            with send_lock:
                conn.send(response)
        except (BrokenPipeError, EOFError, OSError):
            break

    scraper = scrapers._SCRAPER_INSTANCES.get(source)
    if scraper:
        try:
            scraper[1].invalidate_session()
        except Exception:
            logger.exception("worker browser cleanup failed")
    conn.close()


def _process_table() -> dict[int, tuple[int, int, str]]:
    """Return pid -> (ppid, process group, state)."""
    result = {}
    if os.path.isdir("/proc"):
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/stat", encoding="utf-8") as f:
                    fields = f.read().rsplit(")", 1)[1].split()
                result[int(entry)] = (int(fields[1]), int(fields[2]), fields[0])
            except (OSError, ValueError, IndexError):
                continue
        return result
    proc = subprocess.run(["ps", "-eo", "pid=,ppid=,pgid=,stat="], capture_output=True,
                          text=True, timeout=5, check=True)
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) == 4 and all(part.isdigit() for part in parts[:3]):
            pid, ppid, group = map(int, parts[:3])
            result[pid] = (ppid, group, parts[3])
    return result


def _stop_process_tree(process) -> None:
    """Stop only descendants of this worker, including detached Chromium groups."""
    pid = process.pid
    if pid is None:
        return
    if not process.is_alive():
        process.join(timeout=0)
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=10, check=False)
        process.join(timeout=3)
        if process.is_alive():
            raise RuntimeError(f"浏览器工作进程 {pid} 无法终止")
        return

    # Freeze the parent before discovering descendants. Then freeze the newly
    # discovered descendants and rescan: Playwright launches Chromium detached.
    owned = {pid}
    groups = {pid}
    for _ in range(8):
        for target in owned:
            try:
                os.kill(target, signal.SIGSTOP)
            except ProcessLookupError:
                pass
        table = _process_table()
        found = set(owned)
        while True:
            added = {child for child, (parent, _, _) in table.items() if parent in found} - found
            if not added:
                break
            found.update(added)
        groups.update(group for child, (_, group, _) in table.items()
                      if child in found and child == group and group != os.getpgrp())
        if found == owned:
            break
        owned = found
    else:
        raise RuntimeError(f"无法稳定识别浏览器工作进程 {pid} 的子进程")

    for group in groups:
        if group == os.getpgrp():
            continue
        try:
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:
            pass
    for target in owned:
        try:
            os.kill(target, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.join(timeout=5)
    if process.is_alive():
        raise RuntimeError(f"浏览器工作进程 {pid} 无法终止")
    deadline = time.monotonic() + 5
    while True:
        table = _process_table()
        alive = {child for child in owned if child in table and not table[child][2].startswith("Z")}
        if not alive:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError(f"浏览器子进程未退出: {sorted(alive)}")
        time.sleep(0.05)


class BrowserWorker:
    """One persistent worker, replaced only after confirmed process-tree cleanup."""

    def __init__(self, source: str, *, entrypoint=_serve):
        self.source = source
        self._entrypoint = entrypoint
        self.process = None
        self.conn = None
        self._ready = False
        self._stop_lock = threading.Lock()
        self._closed = False
        self._runtime_version = None

    def _start(self) -> None:
        with self._stop_lock:
            if self._closed:
                raise RuntimeError("浏览器工作进程已关闭")
            context = multiprocessing.get_context("spawn")
            parent, child = context.Pipe()
            process = context.Process(target=self._entrypoint, args=(child, self.source), daemon=True)
            try:
                process.start()
            except BaseException:
                parent.close()
                child.close()
                raise
            child.close()
            self.process, self.conn, self._ready = process, parent, False

    def _stop_locked(self) -> None:
        if self.process is None:
            return
        _stop_process_tree(self.process)
        self.conn.close()
        self.process.close()
        self.process, self.conn, self._ready = None, None, False

    def stop(self) -> None:
        with self._stop_lock:
            self._stop_locked()

    def close(self) -> None:
        with self._stop_lock:
            self._closed = True
            self._stop_locked()

    def execute(self, tasks, *, multi_source: bool, timeout: float):
        from config import get_proxy_url
        import scrapers.holland2stay as h2s

        from settings_store import runtime_environment
        env = runtime_environment()
        version = env.get("FLATRADAR_RUNTIME_VERSION")
        if self.process is not None and (not self.process.is_alive() or
                                         (version and version != self._runtime_version)):
            self.stop()
        if self.process is None:
            self._start()
        self._runtime_version = version
        conn = self.conn
        deadline = time.monotonic() + timeout
        try:
            if not self._ready:
                self._receive(conn, deadline, get_proxy_url, expect="ready")
                self._ready = True
            conn.send(("scrape", tasks, multi_source, env,
                       dict(h2s._DETAIL_CACHE) if self.source == "holland2stay" else {}))
            kind, value, detail = self._receive(conn, deadline, get_proxy_url, expect="result")
            if detail is not None:
                h2s.prime_detail_cache(detail)
            if kind == "error":
                raise _restore_exception(value)
            return value
        except (TimeoutError, EOFError, BrokenPipeError, OSError) as exc:
            self.stop()
            raise ScrapeNetworkError(f"{self.source} 浏览器工作进程超时或断开: {exc}") from exc

    def _receive(self, conn, deadline, get_proxy_url, *, expect):
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not conn.poll(remaining):
                raise TimeoutError(f"超过截止时间（{self.source}）")
            message = conn.recv()
            kind = message[0]
            if kind == "log":
                _, name, level, text = message
                logging.getLogger(name).log(level, "%s", text)
            elif kind == "proxy":
                _, source, rotating = message
                try:
                    proxy = get_proxy_url(source, rotating=rotating)
                except Exception as exc:
                    logger.exception("%s 代理选择失败", source)
                    conn.send(("proxy_error", type(exc).__name__))
                else:
                    conn.send(("proxy_result", proxy))
            elif kind == expect or (expect == "result" and kind == "error"):
                return message
            else:
                raise OSError(f"浏览器工作进程协议错误: {kind}")
