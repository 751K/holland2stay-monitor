"""公告逐用户投递记录与精确补发。

背景
----
公告群发不是原子的：外部渠道、APNs、FCM 各自独立，个别用户可能全部失败。
2026-08-28 FCM 全挂那次，事后无法回答「谁收到了」，只能整批重发——已经
收到的人被再打扰一遍。这里守住三件事：

1. 每个目标用户的每个渠道都有落库结果（ok / failed / skipped）；
2. 重试只碰 failed，已成功或按设计跳过的用户不再被打扰；
3. 重试会更新同一行，不会留下互相矛盾的两份记录。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from storage import Storage


class _User:
    def __init__(self, uid, name, *, enabled=True, notify=True):
        self.id = uid
        self.name = name
        self.enabled = enabled
        self.notifications_enabled = notify


class _Notifier:
    def __init__(self, *, has_channels=True, fail=False, calls=None):
        self.has_channels = has_channels
        self._fail = fail
        self._calls = calls if calls is not None else []

    async def send_announcement(self, title, body=""):
        self._calls.append(self)
        if self._fail:
            raise RuntimeError("channel down")
        return True

    async def close(self):
        pass


def _broadcast(tmp_path, monkeypatch, users, *, factory, push=0):
    from app.services import announcement_service as svc

    db = Path(tmp_path) / "t.db"
    st = Storage(db, timezone_str="UTC")

    async def _push(storage, user_id, title, body):
        return push

    monkeypatch.setattr("users.load_users", lambda: users)
    monkeypatch.setattr("notifier.create_user_notifier", factory)
    monkeypatch.setattr("app.db.storage", lambda: st)
    monkeypatch.setattr("mcore.push.dispatch_announcement_to_user", _push)

    res = svc.broadcast("维护通知", "今晚 22:00 起短时维护。")
    return res, Storage(db, timezone_str="UTC")


def _retry(tmp_path, monkeypatch, users, announcement_id, *, factory, push=0):
    from app.services import announcement_service as svc

    db = Path(tmp_path) / "t.db"
    st = Storage(db, timezone_str="UTC")

    async def _push(storage, user_id, title, body):
        return push

    monkeypatch.setattr("users.load_users", lambda: users)
    monkeypatch.setattr("notifier.create_user_notifier", factory)
    monkeypatch.setattr("app.db.storage", lambda: st)
    monkeypatch.setattr("mcore.push.dispatch_announcement_to_user", _push)

    res = svc.retry_failed(announcement_id)
    return res, Storage(db, timezone_str="UTC")


class TestDeliveryRecords:
    def test_successes_and_skips_are_recorded(self, tmp_path, monkeypatch):
        res, db = _broadcast(
            tmp_path, monkeypatch, [_User("a", "A"), _User("b", "B")],
            factory=lambda u: _Notifier(),
        )
        try:
            assert res.announcement_id > 0
            ann = db.get_announcement(res.announcement_id)
            assert ann["recipients"] == 2

            rows = {
                (r["user_id"], r["channel"]): r
                for r in db.list_announcement_deliveries(res.announcement_id)
            }
            assert rows[("a", "external")]["status"] == "ok"
            # 测试库没有设备：推送是 skipped，不是 failed
            assert rows[("a", "push")]["status"] == "skipped"
            assert rows[("a", "push")]["error"] == "no_devices"
            assert rows[("b", "external")]["status"] == "ok"
        finally:
            db.close()

    def test_failure_is_recorded_not_just_counted(self, tmp_path, monkeypatch):
        calls = []
        res, db = _broadcast(
            tmp_path, monkeypatch, [_User("a", "A"), _User("b", "B")],
            factory=lambda u: _Notifier(fail=(u.id == "b"), calls=calls),
        )
        try:
            rows = {
                (r["user_id"], r["channel"]): r
                for r in db.list_announcement_deliveries(res.announcement_id)
            }
            assert rows[("a", "external")]["status"] == "ok"
            assert rows[("b", "external")]["status"] == "failed"
            assert "channel down" in rows[("b", "external")]["error"]
        finally:
            db.close()

    def test_user_without_external_channels_is_skipped(self, tmp_path, monkeypatch):
        res, db = _broadcast(
            tmp_path, monkeypatch, [_User("a", "A")],
            factory=lambda u: _Notifier(has_channels=False),
        )
        try:
            rows = db.list_announcement_deliveries(res.announcement_id)
            external = [r for r in rows if r["channel"] == "external"][0]
            assert external["status"] == "skipped"
            assert external["error"] == "no_channels"
        finally:
            db.close()

    def test_disabled_user_has_no_delivery_rows(self, tmp_path, monkeypatch):
        res, db = _broadcast(
            tmp_path, monkeypatch,
            [_User("a", "A"), _User("b", "B", notify=False)],
            factory=lambda u: _Notifier(),
        )
        try:
            ann = db.get_announcement(res.announcement_id)
            assert ann["recipients"] == 1
            assert ann["skipped_disabled"] == 1
            user_ids = {
                r["user_id"]
                for r in db.list_announcement_deliveries(res.announcement_id)
            }
            assert user_ids == {"a"}
        finally:
            db.close()

    def test_history_stats_count_failed_users(self, tmp_path, monkeypatch):
        res, db = _broadcast(
            tmp_path, monkeypatch, [_User("a", "A"), _User("b", "B")],
            factory=lambda u: _Notifier(fail=(u.id == "b")),
        )
        try:
            row = db.list_announcements()[0]
            assert row["id"] == res.announcement_id
            assert row["external_ok"] == 1
            assert row["failed_users"] == 1
        finally:
            db.close()


class TestRetry:
    def _seed_failed(self, tmp_path, monkeypatch, users):
        calls = []
        res, db = _broadcast(
            tmp_path, monkeypatch, users,
            factory=lambda u: _Notifier(fail=(u.id == "b"), calls=calls),
        )
        db.close()
        return res.announcement_id, calls

    def test_retries_only_the_failed_user(self, tmp_path, monkeypatch):
        users = [_User("a", "A"), _User("b", "B")]
        ann_id, first_calls = self._seed_failed(tmp_path, monkeypatch, users)
        assert len(first_calls) == 2

        retry_calls = []
        res, db = _retry(
            tmp_path, monkeypatch, users, ann_id,
            factory=lambda u: _Notifier(calls=retry_calls),
        )
        try:
            assert len(retry_calls) == 1, "已成功的用户被再次打扰"
            assert res.recipients == 1

            rows = {
                (r["user_id"], r["channel"]): r
                for r in db.list_announcement_deliveries(ann_id)
            }
            assert rows[("b", "external")]["status"] == "ok", "重试结果没有覆盖 failed"
            assert rows[("a", "external")]["status"] == "ok"
            # 同一 (user, channel) 只有一行
            assert len(db.list_announcement_deliveries(ann_id)) == 4
        finally:
            db.close()

    def test_retry_marks_users_disabled_since_as_skipped(self, tmp_path, monkeypatch):
        ann_id, _ = self._seed_failed(
            tmp_path, monkeypatch, [_User("a", "A"), _User("b", "B")],
        )
        # 发送之后 b 关掉了通知
        users = [_User("a", "A"), _User("b", "B", notify=False)]
        res, db = _retry(
            tmp_path, monkeypatch, users, ann_id,
            factory=lambda u: _Notifier(),
        )
        try:
            row = [
                r for r in db.list_announcement_deliveries(ann_id)
                if r["user_id"] == "b" and r["channel"] == "external"
            ][0]
            assert row["status"] == "skipped"
            assert row["error"] == "notifications_disabled"
            assert res.skipped_disabled == 1
        finally:
            db.close()

    def test_retry_nothing_failed_is_a_noop(self, tmp_path, monkeypatch):
        res, db = _broadcast(
            tmp_path, monkeypatch, [_User("a", "A")],
            factory=lambda u: _Notifier(),
        )
        ann_id = res.announcement_id
        db.close()

        calls = []
        again, db2 = _retry(
            tmp_path, monkeypatch, [_User("a", "A")], ann_id,
            factory=lambda u: _Notifier(calls=calls),
        )
        try:
            assert calls == []
            assert again.recipients == 0
        finally:
            db2.close()

    def test_unknown_announcement_is_rejected(self, tmp_path, monkeypatch):
        from app.services.announcement_service import retry_failed

        db = Path(tmp_path) / "t.db"
        st = Storage(db, timezone_str="UTC")
        monkeypatch.setattr("app.db.storage", lambda: st)
        try:
            with pytest.raises(ValueError):
                retry_failed(999)
        finally:
            st.close()

    def test_pending_rows_from_an_interrupted_broadcast_are_retried(
        self, tmp_path, monkeypatch,
    ):
        """群发中途被杀：剩下的用户只有 pending 行，重试必须补上。

        没有预先写 pending 的话，这些用户连「该被投递」的记录都没有，
        事后无从知道漏了谁。
        """
        db_path = Path(tmp_path) / "t.db"
        st = Storage(db_path, timezone_str="UTC")
        try:
            ann_id = st.create_announcement("维护通知", "正文", recipients=1)
            st.upsert_announcement_delivery(ann_id, "b", "external", "pending")
            st.upsert_announcement_delivery(ann_id, "b", "push", "pending")
        finally:
            st.close()

        calls = []
        res, db = _retry(
            tmp_path, monkeypatch, [_User("b", "B")], ann_id,
            factory=lambda u: _Notifier(calls=calls),
        )
        try:
            assert len(calls) == 1, "pending 没有被当成待补发"
            assert res.recipients == 1
            rows = db.list_announcement_deliveries(ann_id)
            assert {r["channel"]: r["status"] for r in rows} == {
                "external": "ok",
                "push": "skipped",
            }
        finally:
            db.close()


class TestRoutes:
    def test_broadcast_response_carries_announcement_id(
        self, admin_client, monkeypatch,
    ):
        from app.services import announcement_service as svc

        monkeypatch.setattr(svc, "broadcast", lambda *a, **k: svc.AnnouncementResult(
            announcement_id=7, recipients=2,
        ))
        r = admin_client.post(
            "/api/announcement", json={"title": "hi"},
            headers={"X-CSRF-Token": "test_csrf"},
        )
        assert r.status_code == 200
        assert r.get_json()["announcement_id"] == 7

    def test_history_requires_admin(self, client):
        assert client.get("/api/announcements").status_code in (401, 403)

    def test_history_returns_recent(self, admin_client):
        r = admin_client.get("/api/announcements")
        assert r.status_code == 200
        assert isinstance(r.get_json()["announcements"], list)

    def test_system_page_renders_history(self, admin_client):
        r = admin_client.get("/system")
        assert r.status_code == 200, "系统页模板渲染失败"

    def test_retry_requires_admin(self, client):
        r = client.post("/api/announcements/1/retry")
        assert r.status_code in (401, 403)

    def test_retry_requires_csrf(self, admin_client):
        r = admin_client.post("/api/announcements/1/retry")
        assert r.status_code == 403

    def test_retry_returns_result(self, admin_client, monkeypatch):
        from app.services import announcement_service as svc

        monkeypatch.setattr(svc, "retry_failed", lambda *a, **k: svc.AnnouncementResult(
            announcement_id=3, recipients=1, channel_ok=1,
        ))
        r = admin_client.post(
            "/api/announcements/3/retry", json={},
            headers={"X-CSRF-Token": "test_csrf"},
        )
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        assert data["announcement_id"] == 3
