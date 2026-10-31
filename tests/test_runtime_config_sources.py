"""Runtime settings have stable sources; other tiers only change on restart."""
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from dotenv import load_dotenv

import settings_store as ss
from storage import Storage


@pytest.fixture
def sources(tmp_path, monkeypatch):
    env = tmp_path / '.env'
    env.write_text('', encoding='utf-8')
    st = Storage(tmp_path / 'test.db')
    monkeypatch.setattr(ss, '_external_runtime', {})
    monkeypatch.setattr(ss, '_external_fixed_keys', set())
    monkeypatch.setattr(ss, '_hydrated', set())
    for key in ('CHECK_INTERVAL', 'MIN_INTERVAL', 'BOOKING_RESULT_TIMEOUT_SEC',
                'DB_PATH', 'WEB_PASSWORD'):
        monkeypatch.delenv(key, raising=False)
    yield SimpleNamespace(env=env, storage=st, monkeypatch=monkeypatch)
    st.close()


def test_external_even_when_equal_to_dotenv_survives_migration(sources):
    sources.monkeypatch.setenv('CHECK_INTERVAL', '30')
    ss._external_runtime['CHECK_INTERVAL'] = '30'
    sources.env.write_text('CHECK_INTERVAL=30\n', encoding='utf-8')
    ss.migrate_env_to_db(sources.storage, sources.env)
    sources.storage.set_app_setting('CHECK_INTERVAL', '90')
    ss.hydrate(sources.storage, env_path=sources.env)
    assert os.environ['CHECK_INTERVAL'] == '30'
    assert ss.source_of('CHECK_INTERVAL') == 'env'
    assert 'CHECK_INTERVAL' in ss.runtime_override_keys(sources.env)


def test_dotenv_override_removed_reveals_database(sources):
    sources.storage.set_app_setting('CHECK_INTERVAL', '90')
    sources.env.write_text('CHECK_INTERVAL=22\n', encoding='utf-8')
    ss.hydrate(sources.storage, env_path=sources.env)
    assert os.environ['CHECK_INTERVAL'] == '22'
    assert ss.source_of('CHECK_INTERVAL') == 'env'
    sources.env.write_text('', encoding='utf-8')
    ss.hydrate(sources.storage, env_path=sources.env)
    assert os.environ['CHECK_INTERVAL'] == '90'
    assert ss.source_of('CHECK_INTERVAL') == 'db'
    assert 'CHECK_INTERVAL' not in ss.runtime_override_keys(sources.env)


def test_deleted_database_key_reverts_to_default(sources):
    sources.storage.set_app_setting('CHECK_INTERVAL', '90')
    ss.hydrate(sources.storage, env_path=sources.env)
    assert os.environ['CHECK_INTERVAL'] == '90'
    sources.storage.delete_app_setting('CHECK_INTERVAL')
    ss.hydrate(sources.storage, env_path=sources.env)
    assert 'CHECK_INTERVAL' not in os.environ
    assert ss.source_of('CHECK_INTERVAL') == 'default'


def test_existing_external_wins_after_repeated_dotenv_edits(sources):
    sources.monkeypatch.setenv('CHECK_INTERVAL', '11')
    ss._external_runtime['CHECK_INTERVAL'] = '11'
    sources.storage.set_app_setting('CHECK_INTERVAL', '33')
    for value in ('22', '44'):
        sources.env.write_text(f'CHECK_INTERVAL={value}\n', encoding='utf-8')
        ss.hydrate(sources.storage, env_path=sources.env)
        assert os.environ['CHECK_INTERVAL'] == '11'


def test_reload_does_not_change_deploy_secret_or_tuning(sources):
    for key in ('DB_PATH', 'WEB_PASSWORD', 'BOOKING_RESULT_TIMEOUT_SEC'):
        sources.monkeypatch.setenv(key, 'before')
    sources.env.write_text('DB_PATH=after\nWEB_PASSWORD=after\n'
                           'BOOKING_RESULT_TIMEOUT_SEC=900\nCHECK_INTERVAL=45\n', encoding='utf-8')
    ss.hydrate(sources.storage, env_path=sources.env)
    assert all(os.environ[key] == 'before' for key in
               ('DB_PATH', 'WEB_PASSWORD', 'BOOKING_RESULT_TIMEOUT_SEC'))
    assert os.environ['CHECK_INTERVAL'] == '45'


def test_read_failure_keeps_previous_runtime_values(sources, monkeypatch):
    sources.storage.set_app_setting('CHECK_INTERVAL', '90')
    ss.hydrate(sources.storage, env_path=sources.env)
    def fail(*args):
        raise OSError('unavailable')
    monkeypatch.setattr(ss, '_runtime_dotenv', fail)
    assert ss.hydrate(sources.storage, env_path=sources.env) == 0
    assert os.environ['CHECK_INTERVAL'] == '90'


def test_monitor_child_does_not_mistake_web_db_values_for_external(sources):
    ss._external_runtime['MIN_INTERVAL'] = '25'
    ss._external_runtime['BOOKING_RESULT_TIMEOUT_SEC'] = '500'
    sources.storage.set_app_setting('CHECK_INTERVAL', '90')
    sources.env.write_text('CHECK_INTERVAL=45\n', encoding='utf-8')
    ss.hydrate(sources.storage, env_path=sources.env)
    sources.monkeypatch.setenv('BOOKING_RESULT_TIMEOUT_SEC', 'old-dotenv-value')
    child = ss.monitor_child_environment()
    assert 'CHECK_INTERVAL' not in child
    assert child['MIN_INTERVAL'] == '25'
    assert child['BOOKING_RESULT_TIMEOUT_SEC'] == '500'
    ss._external_runtime.pop('BOOKING_RESULT_TIMEOUT_SEC')
    assert 'BOOKING_RESULT_TIMEOUT_SEC' not in ss.monitor_child_environment()


def test_monitor_service_passes_clean_child_environment(sources, monkeypatch):
    from app.services import monitor_service
    seen = {}
    sources.storage.set_app_setting('CHECK_INTERVAL', '90')
    ss.hydrate(sources.storage, env_path=sources.env)
    monkeypatch.setattr(monitor_service, 'monitor_pid', lambda: None)
    monkeypatch.setattr(monitor_service, 'supervisorctl_available', lambda: False)
    monkeypatch.setattr(monitor_service.subprocess, 'Popen', lambda *args, **kw: seen.update(kw))
    assert monitor_service.start_monitor()['started']
    assert 'CHECK_INTERVAL' not in seen['env']


def test_capture_happens_before_dotenv_and_external_wins(sources, monkeypatch):
    monkeypatch.setattr(ss, '_external_runtime', None)
    monkeypatch.setenv('CHECK_INTERVAL', '11')
    sources.env.write_text('CHECK_INTERVAL=22\n', encoding='utf-8')
    ss.capture_external_runtime()
    load_dotenv(dotenv_path=sources.env)
    ss.hydrate(sources.storage, env_path=sources.env)
    assert os.environ['CHECK_INTERVAL'] == '11'


def test_dotenv_interpolation_uses_effective_sources_not_old_injected_values(sources):
    sources.storage.set_app_setting('MIN_INTERVAL', '15')
    ss.hydrate(sources.storage, env_path=sources.env)
    sources.env.write_text('MIN_INTERVAL=22\nCHECK_INTERVAL=${MIN_INTERVAL}\n', encoding='utf-8')
    ss._external_runtime['MIN_INTERVAL'] = '13'
    ss.hydrate(sources.storage, env_path=sources.env)
    assert os.environ['MIN_INTERVAL'] == '13'
    assert os.environ['CHECK_INTERVAL'] == '13'
    del ss._external_runtime['MIN_INTERVAL']
    ss.hydrate(sources.storage, env_path=sources.env)
    assert os.environ['MIN_INTERVAL'] == '22'
    assert os.environ['CHECK_INTERVAL'] == '22'


def test_dotenv_interpolation_can_use_database_fallback(sources):
    sources.storage.set_app_setting('MIN_INTERVAL', '15')
    sources.env.write_text('CHECK_INTERVAL=${MIN_INTERVAL}\n', encoding='utf-8')
    ss.hydrate(sources.storage, env_path=sources.env)
    assert os.environ['CHECK_INTERVAL'] == '15'


def test_migration_preserves_resolved_dotenv_references(sources):
    sources.env.write_text('MIN_INTERVAL=15\nCHECK_INTERVAL=${MIN_INTERVAL}\n', encoding='utf-8')
    ss.migrate_env_to_db(sources.storage, sources.env)
    ss.hydrate(sources.storage, env_path=sources.env)
    assert sources.storage.get_app_setting('CHECK_INTERVAL') == '15'
    assert os.environ['CHECK_INTERVAL'] == '15'


def test_restarted_monitor_process_receives_fresh_database_settings(sources):
    import json
    import subprocess
    import sys
    sources.storage.set_app_setting('CHECK_INTERVAL', '90')
    ss.hydrate(sources.storage, env_path=sources.env)
    assert os.environ['CHECK_INTERVAL'] == '90'
    sources.storage.set_app_setting('CHECK_INTERVAL', '45')
    child_code = r"""
import json, os, sys
from pathlib import Path
from dotenv import load_dotenv
import settings_store as sources
from storage import Storage
sources.capture_external_runtime()
load_dotenv(dotenv_path=sys.argv[1])
db = Storage(Path(sys.argv[2]))
sources.hydrate(db, env_path=sys.argv[1])
print(json.dumps([os.environ.get('CHECK_INTERVAL'), sources.source_of('CHECK_INTERVAL')]))
db.close()
"""
    path = sources.storage.conn.execute('PRAGMA database_list').fetchone()[2]
    run = subprocess.run([sys.executable, '-c', child_code, str(sources.env), path],
                         env=ss.monitor_child_environment(), capture_output=True,
                         text=True, timeout=15, check=True)
    assert json.loads(run.stdout) == ['45', 'db']


def test_restart_inherits_only_genuine_secret_and_deploy_overrides(sources):
    sources.monkeypatch.setenv('WEB_PASSWORD', 'old-file-value')
    sources.monkeypatch.setenv('DB_PATH', '/old-file-value')
    child = ss.monitor_child_environment()
    assert 'WEB_PASSWORD' not in child and 'DB_PATH' not in child
    ss._external_fixed_keys.update({'WEB_PASSWORD', 'DB_PATH'})
    child = ss.monitor_child_environment()
    assert child['WEB_PASSWORD'] == 'old-file-value'
    assert child['DB_PATH'] == '/old-file-value'


def test_settings_page_warns_for_dotenv_added_after_web_started(admin_client, isolated_data_dir):
    import config
    config.ENV_PATH.write_text('CHECK_INTERVAL=22\n', encoding='utf-8')
    response = admin_client.get('/settings')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'CHECK_INTERVAL' in html
    assert 'external environment or .env' in html or '外部环境变量或 .env' in html
    config.ENV_PATH.write_text('', encoding='utf-8')
    ss._external_runtime['CHECK_INTERVAL'] = '30'
    response = admin_client.get('/settings')
    assert response.status_code == 200
    assert 'CHECK_INTERVAL' in response.get_data(as_text=True)


def test_exported_dotenv_runtime_key_is_reported_and_migrated(sources):
    sources.env.write_text('export CHECK_INTERVAL=22\nWEB_PASSWORD=example\n', encoding='utf-8')
    assert ss.env_overrides(sources.env) == ['CHECK_INTERVAL']
    assert 'CHECK_INTERVAL' in ss.runtime_override_keys(sources.env)
    moved, skipped = ss.migrate_env_to_db(sources.storage, sources.env)
    assert moved == ['CHECK_INTERVAL'] and not skipped
    assert 'CHECK_INTERVAL' not in sources.env.read_text(encoding='utf-8')
    assert 'WEB_PASSWORD=example' in sources.env.read_text(encoding='utf-8')
    ss.hydrate(sources.storage, env_path=sources.env)
    assert os.environ['CHECK_INTERVAL'] == '22'


def test_restarted_monitor_loads_changed_file_secret_without_overriding_external(sources):
    import json
    import subprocess
    import sys
    sources.monkeypatch.setenv('WEB_PASSWORD', 'old-file-secret')
    sources.monkeypatch.setenv('BOOKING_RESULT_TIMEOUT_SEC', '600')
    sources.env.write_text('WEB_PASSWORD=new-file-secret\nBOOKING_RESULT_TIMEOUT_SEC=900\n', encoding='utf-8')
    child_code = r"""
import json, os, sys
from dotenv import load_dotenv
load_dotenv(dotenv_path=sys.argv[1])
print(json.dumps([os.environ.get('WEB_PASSWORD'), os.environ.get('BOOKING_RESULT_TIMEOUT_SEC')]))
"""
    result = subprocess.run([sys.executable, '-c', child_code, str(sources.env)],
                            env=ss.monitor_child_environment(), capture_output=True,
                            text=True, timeout=15, check=True)
    assert json.loads(result.stdout) == ['new-file-secret', '900']
    ss._external_fixed_keys.add('WEB_PASSWORD')
    ss._external_runtime['BOOKING_RESULT_TIMEOUT_SEC'] = '600'
    result = subprocess.run([sys.executable, '-c', child_code, str(sources.env)],
                            env=ss.monitor_child_environment(), capture_output=True,
                            text=True, timeout=15, check=True)
    assert json.loads(result.stdout) == ['old-file-secret', '600']


def test_equal_external_value_is_preserved_through_real_startup_and_migration(sources, monkeypatch):
    monkeypatch.setattr(ss, '_external_runtime', None)
    monkeypatch.setenv('CHECK_INTERVAL', '30')
    sources.env.write_text('CHECK_INTERVAL=30\n', encoding='utf-8')
    ss.capture_external_runtime()
    load_dotenv(dotenv_path=sources.env)
    ss.migrate_env_to_db(sources.storage, sources.env)
    sources.storage.set_app_setting('CHECK_INTERVAL', '90')
    ss.hydrate(sources.storage, env_path=sources.env)
    assert os.environ['CHECK_INTERVAL'] == '30'
    assert ss.source_of('CHECK_INTERVAL') == 'env'


def test_real_monitor_reload_preserves_tiers_and_external_precedence(sources, monkeypatch):
    import config
    from mcore import startup
    path = sources.storage.conn.execute('PRAGMA database_list').fetchone()[2]
    monkeypatch.setattr(config, 'DB_PATH', Path(path))
    monkeypatch.setattr(startup, 'ENV_PATH', sources.env)
    monkeypatch.setenv('CHECK_INTERVAL', '11')
    monkeypatch.setenv('BOOKING_RESULT_TIMEOUT_SEC', '600')
    ss._external_runtime['CHECK_INTERVAL'] = '11'
    sources.storage.set_app_setting('CHECK_INTERVAL', '33')
    sources.storage.set_app_setting('MIN_INTERVAL', '20')
    sources.env.write_text('CHECK_INTERVAL=22\nBOOKING_RESULT_TIMEOUT_SEC=900\n', encoding='utf-8')
    candidate = startup.reload_settings()
    assert 'MIN_INTERVAL' not in os.environ
    ss.commit_runtime(candidate)
    assert os.environ['CHECK_INTERVAL'] == '11'
    assert os.environ['MIN_INTERVAL'] == '20'
    assert os.environ['BOOKING_RESULT_TIMEOUT_SEC'] == '600'
    sources.storage.delete_app_setting('MIN_INTERVAL')
    candidate = startup.reload_settings()
    assert os.environ['MIN_INTERVAL'] == '20'
    ss.commit_runtime(candidate)
    assert 'MIN_INTERVAL' not in os.environ
