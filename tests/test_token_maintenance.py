"""Session cleanup is independent of heartbeat settings and failures are non-fatal."""
from unittest.mock import MagicMock

from mcore import token_maintenance


def test_runs_at_most_daily_and_retries_failures(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(token_maintenance.time, "monotonic", lambda: clock[0])
    storage = MagicMock()
    storage.cleanup_expired_tokens.side_effect = [RuntimeError("db busy"), 1, 0]
    maintenance = token_maintenance.TokenMaintenance()
    maintenance.run_if_due(storage)
    maintenance.run_if_due(storage)
    assert storage.cleanup_expired_tokens.call_count == 2
    clock[0] = token_maintenance._CLEANUP_INTERVAL - 1
    maintenance.run_if_due(storage)
    assert storage.cleanup_expired_tokens.call_count == 2
    clock[0] += 1
    maintenance.run_if_due(storage)
    assert storage.cleanup_expired_tokens.call_count == 3


def test_scheduled_cleanup_removes_old_session_and_device(temp_db):
    tid, _ = temp_db.create_app_token(role="admin", user_id=None)
    temp_db.register_device(app_token_id=tid, device_token="a" * 64)
    with temp_db.conn:
        temp_db.conn.execute("UPDATE app_tokens SET expires_at='2020-01-01T00:00:00Z' WHERE id=?", (tid,))
    token_maintenance.TokenMaintenance().run_if_due(temp_db)
    assert not temp_db.list_devices_for_token(tid)
    assert temp_db.conn.execute("SELECT count(*) FROM app_tokens WHERE id=?", (tid,)).fetchone()[0] == 0
