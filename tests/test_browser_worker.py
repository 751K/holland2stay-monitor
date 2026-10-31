"""Exercise real spawned workers and their process trees without a live website."""
import os
import asyncio
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
import time

import pytest

from mcore.browser_worker import BrowserWorker, _exception_chain, _restore_exception
from scrapers.base import ProxyError, ScrapeNetworkError, is_proxy_account_error
from scrapers.base import ScrapeTask


def _worker_echo(conn, source):
    if os.name == "posix":
        os.setsid()
    conn.send(("ready",))
    while True:
        request = conn.recv()
        if request[0] == "stop":
            return
        conn.send(("result", (os.getpid(), request[1]), None))


def _worker_hangs(conn, source):
    if os.name == "posix":
        os.setsid()
    conn.send(("ready",))
    conn.recv()
    time.sleep(30)


def _worker_requests_proxy(conn, source):
    if os.name == "posix":
        os.setsid()
    conn.send(("ready",))
    while True:
        request = conn.recv()
        conn.send(("proxy", source, True))
        kind, proxy = conn.recv()
        assert kind == "proxy_result"
        conn.send(("result", (proxy, request[3].get("TEST_BROWSER_SETTING")), None))


def _worker_adds_details(conn, source):
    if os.name == "posix":
        os.setsid()
    conn.send(("ready",))
    request = conn.recv()
    detail = dict(request[4])
    detail["fresh"] = {"tenant": "student"}
    conn.send(("result", request[4], detail))
    time.sleep(30)


def _serve_with_proxy_scraper(conn, source):
    import scrapers
    import scrapers.xior
    from models import Listing
    from scrapers.base import AbstractScraper, ScrapeResult

    class TestScraper(AbstractScraper):
        source = "xior"

        def scrape(self, task):
            proxy = scrapers.xior.get_proxy_url("xior", rotating=True)
            if os.environ.get("TEST_PROXY_FAILURE") == "1":
                try:
                    raise RuntimeError("CONNECT tunnel failed, response 402")
                except RuntimeError as exc:
                    raise ProxyError("proxy failed") from exc
            listing = Listing(id=proxy, name="Proxy", status="Available", price_raw=None,
                              available_from=None, features=[], url="", source="xior")
            return ScrapeResult(task=task, listings=[listing], complete=True)

    scrapers.SCRAPER_REGISTRY["xior"] = TestScraper
    from mcore.browser_worker import _serve
    _serve(conn, source)


def test_worker_reuses_process_across_rounds():
    worker = BrowserWorker("xior", entrypoint=_worker_echo)
    try:
        pid1, tasks1 = worker.execute(["first"], multi_source=True, timeout=10)
        pid2, tasks2 = worker.execute(["second"], multi_source=True, timeout=10)
        assert pid1 == pid2 and tasks1 == ["first"] and tasks2 == ["second"]
    finally:
        worker.stop()


def test_real_worker_dispatches_without_opening_a_browser():
    worker = BrowserWorker("missing")
    task = ScrapeTask(source="missing", city_key="a", city_display="A")
    try:
        listings, completeness = worker.execute([task], multi_source=True, timeout=15)
        assert listings == []
        assert completeness == {"missing:A": False}
    finally:
        worker.stop()


def test_timeout_reaps_worker_and_next_call_can_restart():
    worker = BrowserWorker("xior", entrypoint=_worker_hangs)
    with pytest.raises(ScrapeNetworkError, match="超时"):
        worker.execute([], multi_source=False, timeout=1)
    assert worker.process is None
    worker._entrypoint = _worker_echo
    try:
        pid, result = worker.execute(["recovered"], multi_source=False, timeout=10)
        assert pid != os.getpid() and result == ["recovered"]
    finally:
        worker.stop()


def test_repeated_timeouts_do_not_accumulate_workers():
    worker = BrowserWorker("xior", entrypoint=_worker_hangs)
    for _ in range(3):
        with pytest.raises(ScrapeNetworkError, match="超时"):
            worker.execute([], multi_source=False, timeout=0.5)
        assert worker.process is None


def test_closed_worker_cannot_start_again():
    worker = BrowserWorker("xior", entrypoint=_worker_echo)
    worker.close()
    with pytest.raises(RuntimeError, match="已关闭"):
        worker.execute([], multi_source=False, timeout=2)


def test_close_interrupts_inflight_request_without_restarting():
    worker = BrowserWorker("xior", entrypoint=_worker_hangs)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(worker.execute, [], multi_source=False, timeout=15)
        deadline = time.monotonic() + 5
        while worker.process is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert worker.process is not None
        worker.close()
        with pytest.raises(ScrapeNetworkError):
            future.result(timeout=5)
    assert worker.process is None
    with pytest.raises(RuntimeError, match="已关闭"):
        worker.execute([], multi_source=False, timeout=1)


def test_dispatch_outer_timeout_also_stops_worker(monkeypatch):
    from mcore import scrape_round

    finished = threading.Event()

    class StalledTransport:
        def execute(self, *_args, **_kwargs):
            finished.wait(5)
            return [], {}

        def stop(self):
            finished.set()

    worker = StalledTransport()
    monkeypatch.setattr(scrape_round, "get_browser_worker", lambda _source: worker)
    monkeypatch.setattr(scrape_round, "_SOURCE_DISPATCH_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(scrape_round, "_BROWSER_CLEANUP_GRACE_SEC", 0.05)

    async def run():
        with pytest.raises(ScrapeNetworkError, match="超时"):
            await scrape_round.dispatch_scrape_tasks_async(
                asyncio.get_running_loop(), [], isolated=True, browser_source="xior",
            )

    asyncio.run(run())
    assert finished.is_set()


def test_proxy_error_cause_chain_preserves_account_classification():
    try:
        try:
            raise RuntimeError("CONNECT tunnel failed, response 402")
        except RuntimeError as exc:
            raise ProxyError("proxy failed") from exc
    except ProxyError as exc:
        restored = _restore_exception(_exception_chain(exc))
    assert isinstance(restored, ProxyError)
    assert is_proxy_account_error(restored)


def test_proxy_selection_and_environment_follow_parent(monkeypatch):
    import config

    calls = []

    def proxy(source, *, rotating=False):
        calls.append((source, rotating))
        return f"socks5://proxy-{len(calls)}:1080"

    monkeypatch.setattr(config, "get_proxy_url", proxy)
    monkeypatch.setenv("TEST_BROWSER_SETTING", "first")
    worker = BrowserWorker("xior", entrypoint=_worker_requests_proxy)
    try:
        first = worker.execute([], multi_source=False, timeout=10)
        monkeypatch.setenv("TEST_BROWSER_SETTING", "second")
        second = worker.execute([], multi_source=False, timeout=10)
        assert first == ("socks5://proxy-1:1080", "first")
        assert second == ("socks5://proxy-2:1080", "second")
        assert calls == [("xior", True), ("xior", True)]
    finally:
        worker.stop()


def test_h2s_detail_cache_is_seeded_and_returned(monkeypatch):
    import scrapers.holland2stay as h2s

    monkeypatch.setattr(h2s, "_DETAIL_CACHE", {"old": {"tenant": "working"}})
    worker = BrowserWorker("holland2stay", entrypoint=_worker_adds_details)
    try:
        sent_cache = worker.execute([], multi_source=False, timeout=10)
        assert sent_cache == {"old": {"tenant": "working"}}
        assert h2s._DETAIL_CACHE["fresh"] == {"tenant": "student"}
    finally:
        worker.close()


def test_real_worker_uses_parent_proxy_and_preserves_error_chain(monkeypatch):
    import config

    calls = []

    def proxy(source, *, rotating=False):
        calls.append((source, rotating))
        return "socks5://selected:1080"

    monkeypatch.setattr(config, "get_proxy_url", proxy)
    monkeypatch.setenv("TEST_PROXY_FAILURE", "0")
    worker = BrowserWorker("xior", entrypoint=_serve_with_proxy_scraper)
    task = ScrapeTask(source="xior", city_key="a", city_display="A")
    try:
        listings, _ = worker.execute([task], multi_source=True, timeout=10)
        assert listings[0].id == "socks5://selected:1080"
        monkeypatch.setenv("TEST_PROXY_FAILURE", "1")
        with pytest.raises(ProxyError) as error:
            worker.execute([task], multi_source=True, timeout=10)
        assert is_proxy_account_error(error.value)
        assert calls == [("xior", True), ("xior", True)]
    finally:
        worker.close()


def test_sigterm_uses_graceful_monitor_shutdown(monkeypatch):
    import signal
    import monitor

    handlers = {}
    monkeypatch.setattr(monitor.signal, "signal", lambda sig, handler: handlers.update({sig: handler}))
    monitor._setup_signals(None)
    with pytest.raises(KeyboardInterrupt):
        handlers[signal.SIGTERM](signal.SIGTERM, None)


def test_windows_monitor_stop_targets_process_tree(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from app.services import monitor_service

    run = Mock()
    monkeypatch.setattr(monitor_service, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(monitor_service.subprocess, "run", run)
    monitor_service.terminate_process(1234)
    assert run.call_args.args[0] == ["taskkill", "/PID", "1234", "/T", "/F"]
    assert run.call_args.kwargs["check"] is True


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group behavior")
def test_cleanup_reaps_detached_descendant():
    from mcore.browser_worker import _stop_process_tree
    import multiprocessing

    parent, child = multiprocessing.get_context("spawn").Pipe()
    process = multiprocessing.get_context("spawn").Process(target=_spawn_detached, args=(child,))
    process.start()
    child.close()
    assert parent.poll(10)
    descendant = parent.recv()
    try:
        _stop_process_tree(process)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(descendant, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            pytest.fail("detached child survived worker cleanup")
    finally:
        if process.is_alive():
            process.kill()
            process.join()
        parent.close()


def _spawn_detached(conn):
    os.setsid()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                             start_new_session=True)
    conn.send(child.pid)
    time.sleep(30)


def _worker_stuck_browser(conn, source):
    from pathlib import Path
    from playwright.sync_api import sync_playwright
    from mcore.browser_worker import _process_table

    if os.name == "posix":
        os.setsid()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.environ["TEST_CHROMIUM_BINARY"], headless=True,
            args=["--no-sandbox"],
        )
        page = browser.new_page()
        table = _process_table()
        descendants = {os.getpid()}
        while True:
            found = {pid for pid, (ppid, _, _) in table.items() if ppid in descendants}
            if found <= descendants:
                break
            descendants.update(found)
        Path(source).write_text(
            "\n".join(str(pid) for pid in descendants if pid != os.getpid()), encoding="utf-8"
        )
        conn.send(("ready",))
        conn.recv()
        page.evaluate("() => new Promise(() => {})")


def _real_serve_with_test_scraper(conn, source):
    import scrapers
    from models import Listing
    from scrapers.base import AbstractScraper, ScrapeResult

    class TestScraper(AbstractScraper):
        source = "xior"

        def scrape(self, task):
            if os.environ.get("TEST_BROWSER_HANG") == "1":
                from pathlib import Path
                from playwright.sync_api import sync_playwright
                from mcore.browser_worker import _process_table

                playwright = sync_playwright().start()
                browser = playwright.chromium.launch(
                    executable_path=os.environ["TEST_CHROMIUM_BINARY"], headless=True,
                    args=["--no-sandbox"],
                )
                page = browser.new_page()
                table = _process_table()
                descendants = {os.getpid()}
                while True:
                    found = {pid for pid, (ppid, _, _) in table.items() if ppid in descendants}
                    if found <= descendants:
                        break
                    descendants.update(found)
                Path(os.environ["TEST_BROWSER_PID_FILE"]).write_text(
                    "\n".join(str(pid) for pid in descendants if pid != os.getpid()),
                    encoding="utf-8",
                )
                page.evaluate("() => new Promise(() => {})")
            listing = Listing(id="recovered", name="Recovered", status="Available",
                              price_raw=None, available_from=None, features=[], url="",
                              source="xior")
            return ScrapeResult(task=task, listings=[listing], complete=True)

    scrapers.SCRAPER_REGISTRY["xior"] = TestScraper
    from mcore.browser_worker import _serve
    _serve(conn, source)


@pytest.mark.skipif(
    os.environ.get("TEST_CHROMIUM_BINARY") is None or os.name != "posix",
    reason="requires an explicitly supplied Chromium binary",
)
def test_real_browser_hang_is_terminated(tmp_path):
    marker = tmp_path / "browser-pids"
    worker = BrowserWorker(str(marker), entrypoint=_worker_stuck_browser)
    with pytest.raises(ScrapeNetworkError, match="超时"):
        worker.execute([], multi_source=False, timeout=15)
    assert marker.exists(), "browser did not start before the deadline"
    pids = [int(value) for value in marker.read_text().splitlines()]
    assert pids, "browser startup did not create any subprocesses"
    from mcore.browser_worker import _process_table
    table = _process_table()
    assert all(pid not in table or table[pid][2].startswith("Z") for pid in pids)


@pytest.mark.skipif(
    os.environ.get("TEST_CHROMIUM_BINARY") is None or os.name != "posix",
    reason="requires an explicitly supplied Chromium binary",
)
def test_real_scrape_worker_recovers_after_browser_hang(tmp_path, monkeypatch):
    marker = tmp_path / "real-worker-pids"
    monkeypatch.setenv("TEST_BROWSER_HANG", "1")
    monkeypatch.setenv("TEST_BROWSER_PID_FILE", str(marker))
    worker = BrowserWorker("xior", entrypoint=_real_serve_with_test_scraper)
    task = ScrapeTask(source="xior", city_key="a", city_display="A")
    try:
        with pytest.raises(ScrapeNetworkError, match="超时"):
            worker.execute([task], multi_source=True, timeout=15)
        assert marker.exists()
        monkeypatch.setenv("TEST_BROWSER_HANG", "0")
        listings, completeness = worker.execute([task], multi_source=True, timeout=15)
        assert [listing.id for listing in listings] == ["recovered"]
        assert completeness == {"xior:A": True}
        from mcore.browser_worker import _process_table
        table = _process_table()
        assert all(int(pid) not in table or table[int(pid)][2].startswith("Z")
                   for pid in marker.read_text().splitlines())
    finally:
        worker.close()


@pytest.mark.skipif(
    os.environ.get("TEST_CHROMIUM_BINARY") is None or os.name != "posix",
    reason="requires an explicitly supplied Chromium binary",
)
def test_monitor_dispatch_recovers_after_real_browser_hang(tmp_path, monkeypatch):
    from mcore import scrape_round

    marker = tmp_path / "dispatch-browser-pids"
    monkeypatch.setenv("TEST_BROWSER_HANG", "1")
    monkeypatch.setenv("TEST_BROWSER_PID_FILE", str(marker))
    worker = BrowserWorker("xior", entrypoint=_real_serve_with_test_scraper)
    monkeypatch.setattr(scrape_round, "get_browser_worker", lambda _source: worker)
    monkeypatch.setattr(scrape_round, "_SOURCE_DISPATCH_TIMEOUT_SEC", 15)
    task = ScrapeTask(source="xior", city_key="a", city_display="A")

    async def run():
        loop = asyncio.get_running_loop()
        with pytest.raises(ScrapeNetworkError, match="超时"):
            await scrape_round.dispatch_scrape_tasks_async(
                loop, [task], isolated=True, browser_source="xior", multi_source=True,
            )
        assert marker.exists()
        monkeypatch.setenv("TEST_BROWSER_HANG", "0")
        listings, completeness = await scrape_round.dispatch_scrape_tasks_async(
            loop, [task], isolated=True, browser_source="xior", multi_source=True,
        )
        assert [listing.id for listing in listings] == ["recovered"]
        assert completeness == {"xior:A": True}

    try:
        asyncio.run(run())
    finally:
        worker.close()
