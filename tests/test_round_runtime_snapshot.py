"""A queued operation retains its round configuration across publication."""
import asyncio
from types import MappingProxyType

import pytest

import monitor
import settings_store as settings
from env_utils import env_get
from mcore import proxy


def candidate(value):
    return settings.RuntimeCandidate(MappingProxyType({'CHECK_INTERVAL': value}), MappingProxyType({}))


def test_snapshot_is_immutable_and_version_does_not_encode_secrets(monkeypatch):
    monkeypatch.setenv('CAPTCHA_API_KEY', 'private-value')
    settings.commit_runtime(candidate('60'))
    snap = settings.capture_runtime_snapshot()
    assert 'private-value' not in snap.version and 'private-value' not in repr(snap)
    with pytest.raises(TypeError):
        snap.environment['CHECK_INTERVAL'] = '80'
    settings.commit_runtime(candidate('80'))
    assert settings.capture_runtime_snapshot().version != snap.version
    with settings.use_runtime_snapshot(snap):
        assert env_get('CHECK_INTERVAL') == '60'
        assert settings.runtime_environment()['FLATRADAR_RUNTIME_VERSION'] == snap.version
    assert env_get('CHECK_INTERVAL') == '80'


def test_queued_thread_and_proxy_use_round_snapshot_but_live_cooldown(monkeypatch):
    monkeypatch.setenv('HTTPS_PROXY', 'http://one:1234')
    monkeypatch.setenv('SCRAPE_PROXIES_FALLBACK', 'http://two:1234')
    monkeypatch.setattr(proxy, '_proxy_cooldown_until', {})
    settings.commit_runtime(candidate('60'))
    snap = settings.capture_runtime_snapshot()
    with settings.use_runtime_snapshot(snap):
        task = settings.context_job(lambda: (env_get('CHECK_INTERVAL'), proxy.get_proxy_url()))
    settings.commit_runtime(candidate('80'))
    monkeypatch.setenv('HTTPS_PROXY', 'http://new:1234')
    proxy._proxy_cooldown_until['http://one:1234'] = float('inf')
    async def execute():
        return await asyncio.get_running_loop().run_in_executor(None, task)
    assert asyncio.run(execute()) == ('60', 'http://two:1234')


def test_monitor_round_keeps_snapshot_when_reload_occurs(monkeypatch):
    settings.commit_runtime(candidate('60'))
    async def body(*args, **kwargs):
        before = settings.capture_runtime_snapshot()
        settings.commit_runtime(candidate('80'))
        await asyncio.sleep(0)
        assert env_get('CHECK_INTERVAL') == '60'
        assert settings.capture_runtime_snapshot() is before
        return before.version
    monkeypatch.setattr(monitor, '_run_once_snapshot', body)
    old_version = asyncio.run(monitor.run_once(None, None, []))
    assert old_version != settings.capture_runtime_snapshot().version
    assert env_get('CHECK_INTERVAL') == '80'


def _browser_probe(conn, source):
    import os
    if os.name == 'posix':
        os.setsid()
    conn.send(('ready',))
    while True:
        try:
            _, _, _, env, _ = conn.recv()
        except EOFError:
            break
        conn.send(('result', (os.getpid(), env['CHECK_INTERVAL'], env.get('FLATRADAR_RUNTIME_VERSION')), None))


def _booking_probe(conn, source):
    import os
    if os.name == 'posix':
        os.setsid()
    conn.send(('ready',))
    while True:
        try:
            _, _, env = conn.recv()
        except EOFError:
            break
        conn.send(('result', (os.getpid(), env['CHECK_INTERVAL'], env.get('FLATRADAR_RUNTIME_VERSION')), None))


def test_workers_receive_new_snapshot_without_restart_for_request_settings():
    from mcore.browser_worker import BrowserWorker
    from mcore.booking_worker import BookingWorker

    settings.commit_runtime(candidate('60'))
    old = settings.capture_runtime_snapshot()
    browser = BrowserWorker('xior', entrypoint=_browser_probe)
    booking = BookingWorker('u', entrypoint=_booking_probe)
    try:
        with settings.use_runtime_snapshot(old):
            b1 = browser.execute([], multi_source=False, timeout=10)
            u1 = booking.run('prewarm', {}, timeout=10)
            settings.commit_runtime(candidate('80'))
            assert browser.execute([], multi_source=False, timeout=10) == b1
            assert booking.run('book', {}, timeout=10) == u1
        new = settings.capture_runtime_snapshot()
        with settings.use_runtime_snapshot(new):
            b2 = browser.execute([], multi_source=False, timeout=10)
            u2 = booking.run('book', {}, timeout=10)
        assert b1[1:] == u1[1:] == ('60', old.version)
        assert b2[1:] == u2[1:] == ('80', new.version)
        assert b1[0] == b2[0] and u1[0] == u2[0]
    finally:
        browser.close()
        booking.close()


def test_versioned_round_reuses_compatible_unversioned_worker():
    from mcore.browser_worker import BrowserWorker

    settings.commit_runtime(candidate('60'))
    worker = BrowserWorker('xior', entrypoint=_browser_probe)
    try:
        before = worker.execute([], multi_source=False, timeout=10)
        snapshot = settings.capture_runtime_snapshot()
        with settings.use_runtime_snapshot(snapshot):
            after = worker.execute([], multi_source=False, timeout=10)
        assert before[2] is None and after[2] == snapshot.version
        assert before[0] == after[0]
    finally:
        worker.close()


def _blocking_booking_probe(conn, source):
    import os
    import time
    from pathlib import Path
    if os.name == 'posix':
        os.setsid()
    conn.send(('ready',))
    while True:
        try:
            action, payload, env = conn.recv()
        except EOFError:
            break
        if action == 'book':
            Path(payload['started']).write_text('started')
            while not Path(payload['release']).exists():
                time.sleep(.01)
        conn.send(('result', (os.getpid(), env.get('BROWSER_BLOCK_RESOURCES')), None))


def test_launch_change_waits_for_booking_to_finish(tmp_path):
    import threading
    import time
    from mcore.booking_worker import BookingWorker

    settings.commit_runtime(candidate('60'))
    original = settings.capture_runtime_snapshot()
    values = dict(original.environment)
    values['BROWSER_BLOCK_RESOURCES'] = 'changed'
    changed = settings.RuntimeSnapshot('new-version', MappingProxyType(values))
    worker = BookingWorker('u', entrypoint=_blocking_booking_probe)
    started, release = tmp_path / 'started', tmp_path / 'release'
    result = {}
    def run_book():
        with settings.use_runtime_snapshot(original):
            result['book'] = worker.run('book', {'started': str(started), 'release': str(release)}, timeout=10)
    def run_new():
        with settings.use_runtime_snapshot(changed):
            result['new'] = worker.run('prewarm', {}, timeout=10)
    first = threading.Thread(target=run_book)
    second = threading.Thread(target=run_new)
    try:
        first.start()
        deadline = time.monotonic() + 10
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert started.exists()
        pid = worker.process.pid
        second.start()
        time.sleep(.2)
        assert first.is_alive() and second.is_alive() and worker.process.pid == pid
        release.write_text('continue')
        first.join(10)
        second.join(10)
        assert not first.is_alive() and not second.is_alive()
        assert result['book'][0] == pid
        assert result['new'][0] != pid and result['new'][1] == 'changed'
    finally:
        release.touch()
        first.join(10)
        if second.ident is not None:
            second.join(10)
        worker.close()
