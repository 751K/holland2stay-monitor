"""管理员公告群发测试。

公告存在的理由：在此之前系统里唯一能群发的是 ``send_error``，它会顶着
**"Monitor Error"** 的标题送到用户手机上。用它发「近期在扩展房源覆盖」这种
说明既误导用户，又稀释真告警的可信度——告警一旦经常不是故障，下次真出事就
没人当回事。

这里守三条：
1. **关闭通知的用户一个都不碰**——他们明确表达过不想被打扰。
2. **单个用户失败不能中断群发**，否则排在后面的人一条都收不到。
3. **公告类型必须对用户可见**，否则它在 App Alerts 里会被当系统事件滤掉。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from app.services.announcement_service import (
    MAX_BODY,
    MAX_TITLE,
    AnnouncementResult,
    broadcast,
)


class _User:
    def __init__(self, uid, name, *, enabled=True, notify=True):
        self.id = uid
        self.name = name
        self.enabled = enabled
        self.notifications_enabled = notify


class _Notifier:
    sent: list = []

    def __init__(self, fail=False):
        self._fail = fail

    async def send_announcement(self, title, body=""):
        if self._fail:
            raise RuntimeError("channel down")
        _Notifier.sent.append((title, body))
        return True

    async def close(self):
        pass


@pytest.fixture(autouse=True)
def _reset():
    _Notifier.sent = []
    yield
    _Notifier.sent = []


def _run(users, *, dry_run=False, notifier_factory=None, push=0, tmp_path=None):
    from storage import Storage
    st = Storage(Path(tmp_path) / "t.db", timezone_str="UTC")

    async def _push_one(storage, user_id, title, body):
        return push

    factory = notifier_factory or (lambda u: _Notifier())
    with patch("users.load_users", return_value=users), \
         patch("notifier.create_user_notifier", side_effect=factory), \
         patch("app.db.storage", return_value=st), \
         patch("mcore.push.dispatch_announcement_to_user", side_effect=_push_one):
        try:
            return broadcast("系统调试中", "近期正在扩展房源覆盖范围。",
                             dry_run=dry_run), st
        finally:
            pass


class TestReach:
    def test_skips_users_with_notifications_off(self, tmp_path):
        users = [_User("a", "A"), _User("b", "B", notify=False), _User("c", "C")]
        res, st = _run(users, tmp_path=tmp_path)
        try:
            assert res.recipients == 2
            assert res.skipped_disabled == 1
            assert len(_Notifier.sent) == 2
        finally:
            st.close()

    def test_skips_disabled_users(self, tmp_path):
        users = [_User("a", "A"), _User("b", "B", enabled=False)]
        res, st = _run(users, tmp_path=tmp_path)
        try:
            assert res.recipients == 1
        finally:
            st.close()

    def test_dry_run_sends_nothing(self, tmp_path):
        users = [_User("a", "A"), _User("b", "B", notify=False)]
        res, st = _run(users, dry_run=True, tmp_path=tmp_path)
        try:
            assert res.recipients == 1
            assert res.skipped_disabled == 1
            assert _Notifier.sent == [], "dry_run 不该真发"
            assert res.web_feed == 0
        finally:
            st.close()

    def test_counts_push_devices(self, tmp_path):
        res, st = _run([_User("a", "A"), _User("b", "B")], push=2, tmp_path=tmp_path)
        try:
            assert res.push_devices == 4
        finally:
            st.close()


class TestResilience:
    def test_one_user_failure_does_not_stop_the_rest(self, tmp_path):
        """否则排在失败用户后面的人一条都收不到。"""
        def factory(u):
            return _Notifier(fail=(u.name == "B"))

        users = [_User("a", "A"), _User("b", "B"), _User("c", "C")]
        res, st = _run(users, notifier_factory=factory, tmp_path=tmp_path)
        try:
            assert res.recipients == 3
            assert res.channel_ok == 2
            assert len(res.errors) == 1 and "B" in res.errors[0]
            assert len(_Notifier.sent) == 2
        finally:
            st.close()

    def test_empty_title_is_rejected(self):
        with pytest.raises(ValueError):
            broadcast("   ", "body")

    def test_long_fields_are_truncated(self, tmp_path):
        from storage import Storage
        st = Storage(Path(tmp_path) / "t.db", timezone_str="UTC")
        try:
            with patch("users.load_users", return_value=[_User("a", "A")]), \
                 patch("notifier.create_user_notifier", side_effect=lambda u: _Notifier()), \
                 patch("app.db.storage", return_value=st), \
                 patch("mcore.push.dispatch_announcement_to_user",
                       side_effect=lambda *a, **k: 0):
                broadcast("T" * 500, "B" * 5000)
            title, body = _Notifier.sent[0]
            assert len(title) == MAX_TITLE and len(body) == MAX_BODY
        finally:
            st.close()


class TestWebFeed:
    def test_writes_one_global_row(self, tmp_path):
        res, st = _run([_User("a", "A"), _User("b", "B")], tmp_path=tmp_path)
        try:
            assert res.web_feed == 1
            # 服务会关掉它自己拿到的那个连接（这里被 patch 成了 st），
            # 所以校验要另开一个连到同一个库
            from storage import Storage
            probe = Storage(Path(tmp_path) / "t.db", timezone_str="UTC")
            rows = probe.conn.execute(
                "SELECT type, title, user_id FROM web_notifications"
            ).fetchall()
            probe.close()
            assert len(rows) == 1
            assert rows[0]["type"] == "announcement"
            assert rows[0]["user_id"] == "", "全局可见，不绑定某个用户"
        finally:
            st.close()


class TestVisibility:
    def test_announcement_is_visible_to_users(self):
        """否则它会被 filter_for_user_view 当系统事件滤掉，用户根本看不到。"""
        from app.services.notification_service import USER_ALLOWED_TYPES
        assert "announcement" in USER_ALLOWED_TYPES


class TestNotifierLayer:
    def test_base_notifier_formats_title_and_body(self):
        import asyncio

        from notifier import BaseNotifier

        class _Cap(BaseNotifier):
            def __init__(self):
                super().__init__()
                self.text = None

            async def _send(self, text):
                self.text = text
                return True

            async def close(self):
                pass

        n = _Cap()
        assert asyncio.run(n.send_announcement("标题", "正文")) is True
        assert n.text == "标题\n正文"
        assert "Monitor Error" not in n.text, "公告不该伪装成故障告警"

        asyncio.run(n.send_announcement("只有标题"))
        assert n.text == "只有标题"

    def test_push_payload_is_not_an_error(self):
        from mcore.push import _payload_announcement

        p = _payload_announcement("系统调试中", "正文")
        assert p["kind"] == "announcement"
        assert p["aps"]["alert"]["title"] == "系统调试中"
        assert p["aps"]["thread-id"] == "announcements", "别和故障告警混同一串"


class TestRoute:
    def test_requires_admin(self, client):
        r = client.post("/api/announcement", json={"title": "x"})
        assert r.status_code in (401, 403)

    def test_empty_title_returns_400(self, admin_client):
        r = admin_client.post("/api/announcement", json={"title": ""},
                              headers={"X-CSRF-Token": "test_csrf"})
        assert r.status_code == 400

    def test_requires_csrf(self, admin_client):
        """群发是写操作，必须挡住跨站伪造。"""
        r = admin_client.post("/api/announcement", json={"title": "x"})
        assert r.status_code == 403
