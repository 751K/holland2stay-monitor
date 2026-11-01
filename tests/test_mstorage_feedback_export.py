"""mstorage 反馈写入与 GDPR 导出。

这两块原先在路由里内联 SQL，迁移到存储层后在这里固定行为：
反馈表由 schema 创建、导出只取当前用户且排除已撤销会话 / 已停用设备。
"""

import pytest

from mstorage import Storage


@pytest.fixture
def store(tmp_path):
    s = Storage(tmp_path / "test.db")
    yield s
    s.close()


class TestFeedback:
    def test_table_is_created_by_schema(self, store):
        row = store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='feedback'"
        ).fetchone()
        assert row is not None

    def test_insert_returns_id_and_persists_fields(self, store):
        fid = store.add_feedback(
            kind="bug",
            message="something broke",
            role="user",
            user_id="u1",
            user_name="Alice",
            app_version="2.0.0",
        )
        assert fid > 0
        row = store.conn.execute(
            "SELECT kind, message, role, user_id, user_name, app_version, platform "
            "FROM feedback WHERE id = ?",
            (fid,),
        ).fetchone()
        assert tuple(row) == (
            "bug", "something broke", "user", "u1", "Alice", "2.0.0", "ios",
        )


class TestCounts:
    def test_empty_database(self, store):
        assert store.count_status_changes() == 0
        assert store.count_web_notifications() == 0

    def test_notifications_are_counted(self, store):
        store.add_web_notification(type="a", title="x")
        store.add_web_notification(type="b", title="y")
        assert store.count_web_notifications() == 2


class TestExportUserData:
    def _seed(self, store):
        tok1, _ = store.create_app_token(
            role="user", user_id="u1", device_name="iPhone",
        )
        tok2, _ = store.create_app_token(
            role="user", user_id="u2", device_name="Pixel",
        )
        dev1 = store.register_device(
            app_token_id=tok1, device_token="a" * 32, platform="ios", os_version="26.0",
        )
        store.register_device(
            app_token_id=tok2, device_token="b" * 32, platform="android",
        )
        store.add_web_notification(type="new_listing", title="mine", user_id="u1")
        store.add_web_notification(type="new_listing", title="other", user_id="u2")
        return tok1, tok2, dev1

    def test_scopes_to_user(self, store):
        self._seed(store)
        data = store.export_user_data("u1")

        assert [n["title"] for n in data["notification_history"]] == ["mine"]
        assert [t["device_name"] for t in data["active_tokens"]] == ["iPhone"]
        assert [d["device_token"] for d in data["active_devices"]] == ["a" * 32]
        assert data["active_devices"][0]["os_version"] == "26.0"

    def test_excludes_revoked_tokens_and_disabled_devices(self, store):
        tok1, _, dev1 = self._seed(store)
        store.revoke_app_token(tok1)
        store.disable_device(dev1, reason="test")

        data = store.export_user_data("u1")
        assert data["active_tokens"] == []
        assert data["active_devices"] == []
        # 通知不受会话状态影响，仍应导出。
        assert [n["title"] for n in data["notification_history"]] == ["mine"]
