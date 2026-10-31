"""A reload publishes nothing until configuration and channels are ready."""
import asyncio
import os
import threading
from types import MappingProxyType, SimpleNamespace

import pytest

import config
import monitor
import settings_store as settings
from env_utils import env_get
from mcore import startup
from storage import Storage


@pytest.fixture
def running(tmp_path, monkeypatch):
    path = tmp_path / 'reload.db'
    env = tmp_path / '.env'
    env.write_text('', encoding='utf-8')
    st = Storage(path)
    monkeypatch.setattr(settings, '_external_runtime', {})
    monkeypatch.setattr(settings, '_hydrated', set())
    monkeypatch.setattr(settings, '_active_runtime', None)
    monkeypatch.setattr(startup, 'ENV_PATH', env)
    monkeypatch.setattr(config, 'DB_PATH', path)
    for key in ('CHECK_INTERVAL', 'MIN_INTERVAL', 'H2S_REST_ENABLED'):
        monkeypatch.delenv(key, raising=False)
    st.set_app_settings({'CHECK_INTERVAL': '60', 'MIN_INTERVAL': '20',
                         'H2S_REST_ENABLED': 'false'})
    settings.hydrate(st, env_path=env, activate=True)
    yield SimpleNamespace(db=st, env=env, path=path, monkeypatch=monkeypatch)
    st.close()


class Channel:
    def __init__(self):
        self.closed = False
    async def close(self):
        self.closed = True


def test_invalid_candidate_does_not_change_any_effective_setting(running, monkeypatch):
    old = settings._active_runtime
    old_cfg = config.load_config()
    running.db.set_app_settings({'CHECK_INTERVAL': 'invalid', 'H2S_REST_ENABLED': 'true'})
    monkeypatch.setattr(monitor, 'load_users', lambda: pytest.fail('users should not load'))
    with pytest.raises(ValueError):
        asyncio.run(monitor._prepare_reload())
    assert settings._active_runtime is old
    assert env_get('CHECK_INTERVAL') == os.environ['CHECK_INTERVAL'] == '60'
    assert env_get('H2S_REST_ENABLED') == os.environ['H2S_REST_ENABLED'] == 'false'
    assert old_cfg.check_interval == 60


def test_failed_storage_or_user_loading_keeps_old_values(running, monkeypatch):
    old = settings._active_runtime
    def fail():
        raise OSError('user database unavailable')
    running.db.set_app_setting('CHECK_INTERVAL', '80')
    monkeypatch.setattr(monitor, 'load_users', fail)
    with pytest.raises(OSError):
        asyncio.run(monitor._prepare_reload())
    assert settings._active_runtime is old and env_get('CHECK_INTERVAL') == '60'
    monkeypatch.setattr(monitor, '_reload_settings', fail)
    with pytest.raises(OSError):
        asyncio.run(monitor._prepare_reload())
    assert settings._active_runtime is old and os.environ['CHECK_INTERVAL'] == '60'


def test_notifier_failure_closes_new_channels_and_keeps_old_settings(running, monkeypatch):
    first, partial, old_channel = Channel(), Channel(), Channel()
    users = [SimpleNamespace(id='1', enabled=True), SimpleNamespace(id='2', enabled=True)]
    running.db.set_app_setting('CHECK_INTERVAL', '80')
    monkeypatch.setattr(monitor, 'load_users', lambda: users)
    def create(user, *, created_channels):
        assert env_get('CHECK_INTERVAL') == '60'
        if user.id == '1':
            return first
        created_channels.append(partial)
        raise RuntimeError('notifier failed')
    monkeypatch.setattr(monitor, 'create_user_notifier', create)
    with pytest.raises(RuntimeError, match='notifier failed'):
        asyncio.run(monitor._prepare_reload())
    assert first.closed and partial.closed and not old_channel.closed
    assert env_get('CHECK_INTERVAL') == os.environ['CHECK_INTERVAL'] == '60'


def test_success_publishes_after_preparation_and_removes_deleted_key(running, monkeypatch):
    new_channel = Channel()
    users = [SimpleNamespace(id='1', enabled=True)]
    running.db.set_app_settings({'CHECK_INTERVAL': '80', 'H2S_REST_ENABLED': 'true'})
    running.db.delete_app_setting('MIN_INTERVAL')
    monkeypatch.setattr(monitor, 'load_users', lambda: users)
    def create(user, *, created_channels):
        assert env_get('CHECK_INTERVAL') == os.environ['CHECK_INTERVAL'] == '60'
        assert env_get('H2S_REST_ENABLED') == 'false'
        return new_channel
    monkeypatch.setattr(monitor, 'create_user_notifier', create)
    cfg, channels = asyncio.run(monitor._prepare_reload())
    assert cfg.check_interval == 80 and channels == [(users[0], new_channel)]
    assert env_get('CHECK_INTERVAL') == os.environ['CHECK_INTERVAL'] == '80'
    assert env_get('H2S_REST_ENABLED') == 'true'
    assert env_get('MIN_INTERVAL') is None and 'MIN_INTERVAL' not in os.environ


def test_commit_error_rolls_back_environment_and_disposes_new_notifier(running, monkeypatch):
    old = settings._active_runtime
    new_channel = Channel()
    running.db.set_app_settings({'CHECK_INTERVAL': '80', 'H2S_REST_ENABLED': 'bad\x00value'})
    monkeypatch.setattr(monitor, 'load_users', lambda: [SimpleNamespace(enabled=True)])
    monkeypatch.setattr(monitor, 'create_user_notifier', lambda user, **kw: new_channel)
    with pytest.raises(ValueError):
        asyncio.run(monitor._prepare_reload())
    assert new_channel.closed and settings._active_runtime is old
    assert os.environ['CHECK_INTERVAL'] == env_get('CHECK_INTERVAL') == '60'
    assert os.environ['H2S_REST_ENABLED'] == env_get('H2S_REST_ENABLED') == 'false'


def test_runtime_readers_do_not_see_partial_commit(running, monkeypatch):
    started, release = threading.Event(), threading.Event()
    class SlowEnvironment(dict):
        def __init__(self, values):
            super().__init__(values)
            self.paused = False
        def __setitem__(self, key, value):
            if key == 'CHECK_INTERVAL' and value == '80' and not self.paused:
                self.paused = True
                started.set()
                assert release.wait(5)
            return super().__setitem__(key, value)
    fake = SlowEnvironment(os.environ)
    monkeypatch.setattr(os, 'environ', fake)
    values = {'CHECK_INTERVAL': '80', 'H2S_REST_ENABLED': 'true'}
    candidate = settings.RuntimeCandidate(MappingProxyType(values), MappingProxyType({}))
    errors = []
    def commit():
        try:
            settings.commit_runtime(candidate)
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=commit)
    thread.start()
    try:
        assert started.wait(2)
        assert env_get('CHECK_INTERVAL') == '60'
        assert env_get('H2S_REST_ENABLED') == 'false'
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and not errors
    assert env_get('CHECK_INTERVAL') == '80' and env_get('H2S_REST_ENABLED') == 'true'


def test_partial_channels_inside_a_user_are_closed_on_constructor_failure(running, monkeypatch):
    import notifier
    from users import UserConfig
    telegram = Channel()
    user = UserConfig(id='u1', name='User', enabled=True,
        notification_channels=['telegram', 'email'], telegram_token='token',
        telegram_chat_id='chat', email_mode='custom', email_smtp_host='smtp.example',
        email_to='user@example.com', email_from='sender@example.com')
    running.db.set_app_setting('CHECK_INTERVAL', '80')
    monkeypatch.setattr(monitor, 'load_users', lambda: [user])
    monkeypatch.setattr(notifier, 'TelegramNotifier', lambda *args, **kwargs: telegram)
    def fail_email(*args, **kwargs):
        raise RuntimeError('cannot build email')
    monkeypatch.setattr(notifier, 'EmailNotifier', fail_email)
    with pytest.raises(RuntimeError, match='cannot build email'):
        asyncio.run(monitor._prepare_reload())
    assert telegram.closed
    assert env_get('CHECK_INTERVAL') == os.environ['CHECK_INTERVAL'] == '60'


def test_commit_rejects_non_runtime_keys(running):
    old = settings._active_runtime
    original = os.environ.get('DB_PATH')
    candidate = settings.RuntimeCandidate(MappingProxyType({'DB_PATH': '/unexpected'}),
                                          MappingProxyType({}))
    with pytest.raises(ValueError, match='非运行时'):
        settings.commit_runtime(candidate)
    assert settings._active_runtime is old and os.environ.get('DB_PATH') == original


def test_real_settings_database_failure_keeps_active_snapshot(running, monkeypatch):
    from mstorage._settings import AppSettingOps
    old = settings._active_runtime
    running.db.set_app_setting('CHECK_INTERVAL', '80')
    def fail_db(self):
        raise OSError('database unavailable')
    monkeypatch.setattr(AppSettingOps, 'all_app_settings', fail_db)
    with pytest.raises(OSError, match='database unavailable'):
        asyncio.run(monitor._prepare_reload())
    assert settings._active_runtime is old
    assert env_get('CHECK_INTERVAL') == os.environ['CHECK_INTERVAL'] == '60'


def test_invalid_inactive_platform_configuration_is_rejected(running, monkeypatch):
    running.db.set_app_settings({'CHECK_INTERVAL': '80', 'SOURCES': 'holland2stay',
                                 'XIOR_CITIES': 'missing_separator'})
    monkeypatch.setattr(monitor, 'load_users', lambda: pytest.fail('invalid settings must be rejected'))
    with pytest.raises(ValueError):
        asyncio.run(monitor._prepare_reload())
    assert env_get('CHECK_INTERVAL') == os.environ['CHECK_INTERVAL'] == '60'


def _source_config_probe(conn, source):
    if os.name == 'posix':
        os.setsid()
    conn.send(('ready',))
    while True:
        try:
            _, _, _, env, _ = conn.recv()
        except EOFError:
            break
        conn.send(('result', (env.get('CHECK_INTERVAL'), env.get('H2S_REST_ENABLED')), None))


def _booking_config_probe(conn, source):
    if os.name == 'posix':
        os.setsid()
    conn.send(('ready',))
    while True:
        try:
            _, _, env = conn.recv()
        except EOFError:
            break
        conn.send(('result', (env.get('CHECK_INTERVAL'), env.get('H2S_REST_ENABLED')), None))


def test_both_worker_types_receive_only_published_runtime_configuration(running):
    from mcore.browser_worker import BrowserWorker
    from mcore.booking_worker import BookingWorker
    source = BrowserWorker('xior', entrypoint=_source_config_probe)
    booking = BookingWorker('u1', entrypoint=_booking_config_probe)
    try:
        assert source.execute([], multi_source=False, timeout=10) == ('60', 'false')
        assert booking.run('book', {}, timeout=10) == ('60', 'false')
        running.db.set_app_settings({'CHECK_INTERVAL': '80', 'H2S_REST_ENABLED': 'true'})
        candidate = startup.reload_settings()
        assert source.execute([], multi_source=False, timeout=10) == ('60', 'false')
        assert booking.run('book', {}, timeout=10) == ('60', 'false')
        settings.commit_runtime(candidate)
        assert source.execute([], multi_source=False, timeout=10) == ('80', 'true')
        assert booking.run('book', {}, timeout=10) == ('80', 'true')
    finally:
        source.close()
        booking.close()


def test_reload_freezes_current_values_if_startup_hydration_was_unavailable(running, monkeypatch):
    monkeypatch.setattr(settings, '_active_runtime', None)
    running.db.set_app_settings({'CHECK_INTERVAL': '80', 'H2S_REST_ENABLED': 'bad\x00value'})
    monkeypatch.setattr(monitor, 'load_users', lambda: [])
    with pytest.raises(ValueError):
        asyncio.run(monitor._prepare_reload())
    assert settings._active_runtime is not None
    assert env_get('CHECK_INTERVAL') == os.environ['CHECK_INTERVAL'] == '60'
    assert env_get('H2S_REST_ENABLED') == os.environ['H2S_REST_ENABLED'] == 'false'


@pytest.mark.parametrize('valid', [False, True])
def test_main_loop_switches_config_and_notifiers_only_on_success(running, monkeypatch, valid):
    from users import UserConfig
    owner = UserConfig(id='u1', name='User', enabled=True)
    old_channel, new_channel = Channel(), Channel()
    old_cfg = config.load_config()
    seen = []
    real_trigger = monitor.RestTrigger
    def make_trigger(wake):
        trigger = real_trigger(wake)
        async def idle(get_cfg):
            await asyncio.Event().wait()
        trigger.run = idle
        return trigger
    async def run_once(cfg, storage, notifiers, **kwargs):
        seen.append((cfg.check_interval, env_get('H2S_REST_ENABLED'), notifiers[0][1]))
        if len(seen) == 1:
            running.db.set_app_settings({'CHECK_INTERVAL': '80' if valid else 'invalid',
                                         'H2S_REST_ENABLED': 'true'})
            return {}
        raise asyncio.CancelledError
    async def no_alerts(*args, **kwargs):
        return None
    calls = iter([True])
    monkeypatch.setattr(monitor, 'RestTrigger', make_trigger)
    monkeypatch.setattr(monitor, 'run_once', run_once)
    monkeypatch.setattr(monitor, 'get_interval', lambda cfg: (10, False))
    monkeypatch.setattr(monitor, '_consume_reload_request_file', lambda: next(calls, False))
    monkeypatch.setattr(monitor, '_dispatch_watchdog_alerts', no_alerts)
    monkeypatch.setattr(monitor._geocode, 'geocode_missing', lambda storage: (0, 0))
    monkeypatch.setattr(monitor, 'load_users', lambda: [owner])
    monkeypatch.setattr(monitor, 'create_user_notifier', lambda user, **kw: new_channel)
    async def run():
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(monitor.main_loop(old_cfg, running.db, [(owner, old_channel)]), timeout=5)
    asyncio.run(run())
    assert seen[0] == (60, 'false', old_channel)
    if valid:
        assert seen[1] == (80, 'true', new_channel)
        assert old_channel.closed and not new_channel.closed
    else:
        assert seen[1] == (60, 'false', old_channel)
        assert not old_channel.closed and not new_channel.closed
