"""The panel only claims application after the monitor acknowledges a candidate."""
import json

from mcore import config_status
from settings_store import prepare_runtime, commit_runtime
from storage import Storage


def test_applied_pending_override_and_deleted_key(tmp_path, monkeypatch):
    import settings_store
    env_path = tmp_path / '.env'
    env_path.write_text('MIN_INTERVAL=12\n', encoding='utf-8')
    monkeypatch.setattr(settings_store, '_external_runtime', {})
    monkeypatch.setattr(settings_store, '_external_fixed_keys', set())
    st = Storage(tmp_path / 'settings.db')
    try:
        st.set_app_settings({'CHECK_INTERVAL': '60', 'MIN_INTERVAL': '15'})
        config_status.capture_startup(env_path)
        candidate = prepare_runtime(st, env_path=env_path)
        commit_runtime(candidate)
        config_status.publish(st, candidate, env_path, pid=123)
        status = config_status.read_status(st, st.all_app_settings(), env_path, monitor_pid=123)
        assert status['state'] == 'applied' and status['overridden'] == ['MIN_INTERVAL']
        st.set_app_settings({'CHECK_INTERVAL': '80', 'MIN_INTERVAL': '19'})
        status = config_status.read_status(st, st.all_app_settings(), env_path, monitor_pid=123)
        assert status['state'] == 'pending' and status['pending'] == ['CHECK_INTERVAL']
        candidate = prepare_runtime(st, env_path=env_path)
        config_status.publish(st, candidate, env_path, pid=123)
        st.delete_app_setting('CHECK_INTERVAL')
        assert config_status.read_status(st, st.all_app_settings(), env_path, monitor_pid=123)['pending'] == ['CHECK_INTERVAL']
        assert config_status.read_status(st, st.all_app_settings(), env_path, monitor_pid=456)['state'] == 'unknown'
        assert config_status.read_status(st, st.all_app_settings(), env_path, monitor_pid=None)['state'] == 'stopped'
    finally:
        st.close()


def test_restart_changes_never_store_secret_values(tmp_path, monkeypatch):
    import settings_store
    env_path = tmp_path / '.env'
    env_path.write_text('HTTPS_PROXY=http://alice:old-secret@host:88\nDB_PATH=/old\n', encoding='utf-8')
    monkeypatch.setattr(settings_store, '_external_runtime', {})
    monkeypatch.setattr(settings_store, '_external_fixed_keys', set())
    st = Storage(tmp_path / 'settings.db')
    try:
        config_status.capture_startup(env_path)
        original = prepare_runtime(st, env_path=env_path)
        config_status.publish(st, original, env_path, pid=123)
        env_path.write_text('HTTPS_PROXY=http://bob:new-secret@host:88\nDB_PATH=/new\n', encoding='utf-8')
        status = config_status.read_status(st, st.all_app_settings(), env_path, monitor_pid=123)
        assert status['state'] == 'pending' and status['restart_unknown']
        new = prepare_runtime(st, env_path=env_path)
        config_status.publish(st, new, env_path, pid=123)
        status = config_status.read_status(st, st.all_app_settings(), env_path, monitor_pid=123)
        assert status['restart_required'] == ['DB_PATH', 'HTTPS_PROXY']
        assert status['state'] == 'applied' and not status['restart_unknown']
        raw = st.get_meta(config_status.META_KEY)
        assert 'alice' not in raw and 'secret' not in raw and 'bob' not in raw
        assert set(json.loads(raw)['restart_required']) == {'DB_PATH', 'HTTPS_PROXY'}
    finally:
        st.close()


def test_settings_page_displays_status_without_credentials(admin_client, isolated_data_dir, tmp_path, monkeypatch):
    import app.process_ctrl
    import config
    import settings_store
    env_path = tmp_path / '.env'
    env_path.write_text('HTTPS_PROXY=http://alice:super-secret@host:88\n', encoding='utf-8')
    monkeypatch.setattr(config, 'ENV_PATH', env_path)
    monkeypatch.setattr(settings_store, '_external_runtime', {})
    monkeypatch.setattr(settings_store, '_external_fixed_keys', set())
    monkeypatch.setattr(app.process_ctrl, 'monitor_pid', lambda: 4321)
    st = Storage(config.DB_PATH)
    try:
        st.set_app_setting('CHECK_INTERVAL', '60')
        config_status.capture_startup(env_path)
        candidate = prepare_runtime(st, env_path=env_path)
        config_status.publish(st, candidate, env_path, pid=4321)
        st.set_app_setting('CHECK_INTERVAL', '80')
        response = admin_client.get('/settings')
        assert response.status_code == 200
        assert b'CHECK_INTERVAL' in response.data
        assert b'super-secret' not in response.data and b'alice' not in response.data
    finally:
        st.close()


def test_only_changed_accounts_invalidate_prewarm(monkeypatch):
    from types import SimpleNamespace
    import monitor

    class Cache:
        def __init__(self):
            self.removed = []
        def keys(self):
            return ('same', 'changed', 'removed')
        def invalidate(self, uid):
            self.removed.append(uid)
    def user(uid, password):
        return SimpleNamespace(id=uid, auto_book=SimpleNamespace(enabled=True, email=uid, password=password))
    cache = Cache()
    monkeypatch.setattr(monitor, 'prewarm_cache', cache)
    old = [(user('same', 'p'), None), (user('changed', 'old'), None), (user('removed', 'p'), None)]
    new = [(user('same', 'p'), None), (user('changed', 'new'), None)]
    monitor._invalidate_changed_prewarm(old, new)
    assert set(cache.removed) == {'changed', 'removed'}


def test_logging_level_updates_live_without_changing_error_handler():
    import logging
    from mcore.startup import apply_logging_level
    root = logging.getLogger()
    old_level = root.level
    main, errors = logging.NullHandler(), logging.NullHandler()
    main._monitor_main = True
    main.setLevel(logging.INFO)
    errors.setLevel(logging.WARNING)
    root.addHandler(main)
    root.addHandler(errors)
    try:
        apply_logging_level('DEBUG')
        assert root.level == main.level == logging.DEBUG
        assert errors.level == logging.WARNING
    finally:
        root.removeHandler(main)
        root.removeHandler(errors)
        root.setLevel(old_level)


def test_late_prewarm_cannot_restore_a_changed_account():
    from mcore.prewarm import PrewarmCache

    class Session:
        closed = False
        def close_if_owned(self):
            self.closed = True
    cache = PrewarmCache()
    generation = cache.generation('user')
    cache.invalidate('user')
    old_session = Session()
    assert not cache.set_if_current('user', old_session, generation)
    assert old_session.closed and cache.get('user') is None


def test_fixed_environment_override_is_named_without_values(tmp_path, monkeypatch):
    import settings_store
    env_path = tmp_path / '.env'
    env_path.write_text('HTTPS_PROXY=http://user:password@proxy:80\n', encoding='utf-8')
    monkeypatch.setattr(settings_store, '_external_runtime', {})
    monkeypatch.setattr(settings_store, '_external_fixed_keys', {'HTTPS_PROXY'})
    st = Storage(tmp_path / 'settings.db')
    try:
        config_status.capture_startup(env_path)
        candidate = prepare_runtime(st, env_path=env_path)
        config_status.publish(st, candidate, env_path, pid=123)
        status = config_status.read_status(st, st.all_app_settings(), env_path, monitor_pid=123)
        assert status['fixed_overridden'] == ['HTTPS_PROXY']
        assert 'password' not in st.get_meta(config_status.META_KEY)
    finally:
        st.close()
