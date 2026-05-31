"""mstorage 通知模块单元测试 — add / get / count_unread / mark_read / prune。"""

import pytest
from mstorage import Storage


@pytest.fixture
def store(tmp_path):
    s = Storage(tmp_path / "test.db")
    yield s
    s.close()


class TestAddAndGet:
    def test_add_returns_id(self, store):
        nid = store.add_web_notification(type="new_listing", title="新房源", body="详情")
        assert isinstance(nid, int)
        assert nid > 0

    def test_get_returns_requested_count(self, store):
        store.add_web_notification(type="a", title="first")
        store.add_web_notification(type="b", title="second")
        items = store.get_notifications(limit=10)
        assert len(items) == 2
        titles = {i["title"] for i in items}
        assert titles == {"first", "second"}

    def test_get_respects_limit_and_offset(self, store):
        for i in range(5):
            store.add_web_notification(type="test", title=str(i))
        page1 = store.get_notifications(limit=2, offset=0)
        page2 = store.get_notifications(limit=2, offset=2)
        assert len(page1) == 2
        assert len(page2) == 2
        ids = [p["id"] for p in page1 + page2]
        assert len(set(ids)) == 4  # all distinct


class TestGetNotificationsSince:
    def test_returns_only_newer(self, store):
        id1 = store.add_web_notification(type="a", title="1")
        id2 = store.add_web_notification(type="a", title="2")
        since = store.get_notifications_since(id1)
        assert len(since) == 1
        assert since[0]["id"] == id2

    def test_zero_returns_all(self, store):
        store.add_web_notification(type="a", title="x")
        store.add_web_notification(type="a", title="y")
        since = store.get_notifications_since(0)
        assert len(since) == 2


class TestUnreadAndMarkRead:
    def test_new_notifications_are_unread(self, store):
        store.add_web_notification(type="a", title="x")
        assert store.count_unread_notifications() == 1

    def test_mark_specific_ids_read(self, store):
        id1 = store.add_web_notification(type="a", title="x")
        store.add_web_notification(type="a", title="y")
        store.mark_notifications_read([id1])
        assert store.count_unread_notifications() == 1

    def test_mark_all_read(self, store):
        for _ in range(3):
            store.add_web_notification(type="a", title="x")
        store.mark_notifications_read()  # None = all
        assert store.count_unread_notifications() == 0

    def test_mark_empty_list_noop(self, store):
        store.add_web_notification(type="a", title="x")
        store.mark_notifications_read([])  # no error
        assert store.count_unread_notifications() == 1


class TestPrune:
    def test_prune_keeps_newest(self, store):
        for i in range(10):
            store.add_web_notification(type="a", title=str(i))
        removed = store.prune_notifications(keep=3)
        assert removed == 7
        remaining = store.get_notifications(limit=20)
        assert len(remaining) == 3

    def test_prune_noop_when_under_limit(self, store):
        for i in range(3):
            store.add_web_notification(type="a", title=str(i))
        removed = store.prune_notifications(keep=10)
        assert removed == 0


class TestWithinDaysWindow:
    """within_days 把 App Alerts 工作集压到最近 N 天（旧通知太多会让客户端卡）。"""

    def _insert_at(self, store, *, days_ago: int, title: str):
        store._conn.execute(
            "INSERT INTO web_notifications (created_at, type, title) "
            "VALUES (strftime('%Y-%m-%dT%H:%M:%SZ','now',?), 'new_listing', ?)",
            (f"-{days_ago} days", title),
        )
        store._conn.commit()

    def test_within_days_excludes_old(self, store):
        self._insert_at(store, days_ago=0, title="today")
        self._insert_at(store, days_ago=3, title="recent")
        self._insert_at(store, days_ago=10, title="old")

        win = store.get_notifications(limit=50, within_days=7)
        titles = {r["title"] for r in win}
        assert titles == {"today", "recent"}, titles

    def test_within_days_none_returns_all(self, store):
        self._insert_at(store, days_ago=0, title="new")
        self._insert_at(store, days_ago=100, title="ancient")
        assert len(store.get_notifications(limit=50)) == 2
        assert len(store.get_notifications(limit=50, within_days=None)) == 2

    def test_within_days_with_user_filter(self, store):
        # user 过滤 + 时间窗同时生效
        store._conn.execute(
            "INSERT INTO web_notifications (created_at, type, title, user_id) "
            "VALUES (strftime('%Y-%m-%dT%H:%M:%SZ','now'), 'new_listing', 'mine', 'u1')")
        store._conn.execute(
            "INSERT INTO web_notifications (created_at, type, title, user_id) "
            "VALUES (strftime('%Y-%m-%dT%H:%M:%SZ','now','-30 days'), 'new_listing', 'mine_old', 'u1')")
        store._conn.execute(
            "INSERT INTO web_notifications (created_at, type, title, user_id) "
            "VALUES (strftime('%Y-%m-%dT%H:%M:%SZ','now'), 'new_listing', 'other', 'u2')")
        store._conn.commit()

        rows = store.get_notifications(limit=50, user_id="u1", within_days=7)
        titles = {r["title"] for r in rows}
        assert titles == {"mine"}, titles  # 排除别人的 + 排除 30 天前的
