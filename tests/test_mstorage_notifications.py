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
