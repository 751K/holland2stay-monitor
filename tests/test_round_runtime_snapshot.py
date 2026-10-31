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


def test_workers_receive_round_version_and_restart_on_new_snapshot():
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
        assert b1[0] != b2[0] and u1[0] != u2[0]
    finally:
        browser.close()
        booking.close()


def test_versioned_round_replaces_unversioned_worker():
    from mcore.browser_worker import BrowserWorker

    settings.commit_runtime(candidate('60'))
    worker = BrowserWorker('xior', entrypoint=_browser_probe)
    try:
        before = worker.execute([], multi_source=False, timeout=10)
        snapshot = settings.capture_runtime_snapshot()
        with settings.use_runtime_snapshot(snapshot):
            after = worker.execute([], multi_source=False, timeout=10)
        assert before[2] is None and after[2] == snapshot.version
        assert before[0] != after[0]
    finally:
        worker.close()
