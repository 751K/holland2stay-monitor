"""Expired in-memory push state must not accumulate across users and listings."""
from mcore import push


def test_cleanup_preserves_live_dedup_and_rate_limits(monkeypatch):
    push.reset()
    clock = [0.0]
    monkeypatch.setattr(push.time, "monotonic", lambda: clock[0])
    try:
        assert push._allow_send("old-user", "old-listing", "new")
        clock[0] = 250.0
        assert push._allow_send("live-user", "live-listing", "new")
        clock[0] = 310.0
        assert push._allow_send("another-user", "new-listing", "new")
        assert ("old-user", "old-listing", "new") not in push._dedup
        assert "old-user" not in push._per_user
        assert not push._allow_send("live-user", "live-listing", "new")
        assert len(push._per_user["live-user"]) == 1  # exactly 60 seconds old
    finally:
        push.reset()
