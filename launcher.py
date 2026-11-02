#!/usr/bin/env python3
"""
Launcher — entry point for running FlatRadar on your own computer
(`python launcher.py`) and for the packaged app.
Starts the web panel directly (no subprocess), downloads the browser
engine on first run and starts the monitor alongside it; pass
--no-monitor to only open the panel. Supports --run-monitor flag so the
web panel can spawn a background monitor process in the same frozen
bundle.
"""
import signal
import sys
import threading
import webbrowser
from pathlib import Path


def main():
    frozen = getattr(sys, "frozen", False)

    if frozen:
        assets = Path(sys._MEIPASS).resolve()
        data_dir = Path.home() / ".h2s-monitor"
    else:
        assets = Path(__file__).resolve().parent
        data_dir = assets

    # Ensure persistent directories exist
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "data").mkdir(exist_ok=True)
    (data_dir / "logs").mkdir(exist_ok=True)

    # First run: copy .env.example → .env
    env_file = data_dir / ".env"
    if not env_file.exists():
        example = assets / ".env.example"
        if example.exists():
            env_file.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
            print("[launcher] Created .env — the defaults are fine for running on this computer.")
        else:
            print("[launcher] No .env.example found. Web panel may not work.")

    # Parse host/port from CLI args
    host = "127.0.0.1"
    port = 8088
    for i, a in enumerate(sys.argv):
        if a == "--port" and i + 1 < len(sys.argv):
            port = int(sys.argv[i + 1])
        if a == "--host" and i + 1 < len(sys.argv):
            host = sys.argv[i + 1]

    # --run-monitor: spawned by web panel to run the monitor in a child process.
    # Remove the flag and hand control to monitor.main().
    if "--run-monitor" in sys.argv:
        sys.argv.remove("--run-monitor")
        from monitor import main as monitor_main
        monitor_main()
        return

    from web import app

    # 本地自用只跑这一条命令：浏览器内核和监控进程都在这里备好，
    # 不用知道 monitor.py 是个独立进程，也不用记得先装 Chromium。
    started_monitor = False
    if "--no-monitor" not in sys.argv:
        _ensure_browser()
        started_monitor = _start_monitor()

    # Auto-open browser after a short delay
    def _open_browser():
        import time
        time.sleep(1.5)
        webbrowser.open(f"http://{host}:{port}")

    threading.Thread(target=_open_browser, daemon=True).start()

    # 关窗口是 SIGHUP、kill 是 SIGTERM，默认都直接退出、不走 finally；
    # 换成 SystemExit，下面才能把监控一起停掉
    def _exit(*_):
        raise SystemExit(0)
    for sig in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, sig):
            signal.signal(getattr(signal, sig), _exit)

    print(f"[launcher] Starting web panel at http://{host}:{port}")
    try:
        app.run(host=host, port=port, debug=False, threaded=True)
    finally:
        # 关掉终端就是不用了：自己拉起的监控一起停，别留个后台进程接着抓
        if started_monitor:
            _stop_monitor()


def _ensure_browser() -> None:
    """Cloudflare 后面的平台要用 CloakBrowser；没装就现在下载，免得监控在后台默默卡住。"""
    try:
        import cloakbrowser
        if not cloakbrowser.binary_info().get("installed"):
            print("[launcher] First run: downloading the browser engine (one time, a few minutes)…")
            cloakbrowser.ensure_binary()
            print("[launcher] Browser engine ready.")
    except Exception as e:
        # 装不上不挡启动：不走浏览器的平台照样能抓，日志里会看到具体哪个平台失败
        print(f"[launcher] Could not prepare the browser engine: {e}")
        print("[launcher] Try `python -m cloakbrowser install`; sources behind Cloudflare will fail until then.")
        return
    if sys.platform == "darwin":
        print("[launcher] macOS: a Chromium window may pop up while checking some sites. Leave it open.")


def _start_monitor() -> bool:
    """拉起监控；已经在跑（上次没关、或在别的终端开着）就不再开第二个。"""
    from app.services.monitor_service import MonitorServiceError, start_monitor
    try:
        start_monitor()
    except MonitorServiceError as e:
        print(f"[launcher] Monitor not started: {e}")
        return False
    print("[launcher] Monitor started. Close this window (or press Ctrl+C) to stop everything.")
    return True


def _stop_monitor() -> None:
    from app.services.monitor_service import MonitorServiceError, stop_monitor
    try:
        stop_monitor()
        print("[launcher] Monitor stopped.")
    except MonitorServiceError:
        pass


if __name__ == "__main__":
    from multiprocessing import freeze_support
    freeze_support()
    main()
