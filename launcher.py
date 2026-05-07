#!/usr/bin/env python3
"""
Launcher — packaged app entry point.
Starts the web panel directly (no subprocess). Supports --run-monitor
flag so the web panel can spawn a background monitor process in the
same frozen bundle.
"""
import os
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
            print("[launcher] Created .env in data directory — please configure and restart.")
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

    # Auto-open browser after a short delay
    def _open_browser():
        import time
        time.sleep(1.5)
        webbrowser.open(f"http://{host}:{port}")

    threading.Thread(target=_open_browser, daemon=True).start()

    print(f"[launcher] Starting web panel at http://{host}:{port}")
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
