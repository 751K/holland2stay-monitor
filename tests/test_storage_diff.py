"""
Storage.diff() 测试 —— 全系统的"事件源"。

contract：
- 库中不存在的 id → new_listings + 入 listings 表 + notified=0
- 库中已存在 + status 变化 → status_changes + 更新 listings + 插入 status_changes
- 库中已存在 + status 不变 → 仅更新 last_seen，不产出事件
- 调用幂等：同一批 fresh 第二次调用应该产出 0 新房源 0 变更
- 异常路径：with self._conn 保证事务原子性

附带覆盖：
- mark_notified / mark_notified_batch
- retry_queue 持久化（meta 表）
- reset_all
"""
from __future__ import annotations

import pytest

from models import Listing


def _l(id_, status="Available to book", **overrides):
    """工厂：构造测试用 Listing。"""
    base = dict(
        id=id_,
        name=f"Listing-{id_}",
        status=status,
        price_raw="€700",
        available_from="2030-01-01",
        features=["Type: Studio", "Area: 26 m²"],
        url=f"https://h2s/{id_}",
        city="Eindhoven",
    )
    base.update(overrides)
    return Listing(**base)


# ─── 核心 diff 行为 ─────────────────────────────────────────────────


class TestDiffFirstRun:
    """空库 + 一批 fresh → 全部进 new_listings。"""

    def test_empty_db_returns_all_as_new(self, temp_db):
        fresh = [_l("a"), _l("b"), _l("c")]
        new, changes = temp_db.diff(fresh)
        assert len(new) == 3
        assert {l.id for l in new} == {"a", "b", "c"}
        assert changes == []

    def test_empty_db_persists_to_listings_table(self, temp_db):
        temp_db.diff([_l("a"), _l("b")])
        assert temp_db.count_all() == 2

    def test_notified_starts_at_zero(self, temp_db):
        """新插入的房源 notified=0，monitor 通知发送成功后再标记。"""
        temp_db.diff([_l("a")])
        row = temp_db.get_listing("a")
        assert row["notified"] == 0


class TestDiffSecondRun:
    """二次调用：相同 fresh → 0 new；status 变 → status_change。"""

    def test_unchanged_listings_no_events(self, temp_db):
        fresh = [_l("a"), _l("b")]
        temp_db.diff(fresh)
        # 第二次：同样的输入
        new, changes = temp_db.diff(fresh)
        assert new == []
        assert changes == []

    def test_status_change_detected(self, temp_db):
        temp_db.diff([_l("a", status="Available in lottery")])
        # 状态变了
        new, changes = temp_db.diff([_l("a", status="Available to book")])
        assert new == []
        assert len(changes) == 1
        listing, old, new_status = changes[0]
        assert listing.id == "a"
        assert old == "Available in lottery"
        assert new_status == "Available to book"

    def test_status_change_persists_to_status_changes_table(self, temp_db):
        temp_db.diff([_l("a", status="X")])
        temp_db.diff([_l("a", status="Y")])
        recent = temp_db.get_recent_changes(hours=24)
        assert len(recent) == 1
        assert recent[0]["listing_id"] == "a"
        assert recent[0]["old_status"] == "X"
        assert recent[0]["new_status"] == "Y"

    def test_last_seen_updated_on_unchanged_status(self, temp_db):
        """status 没变也要更新 last_seen（用于检测"什么时候开始消失"）。"""
        import time
        temp_db.diff([_l("a", status="X")])
        first_seen = temp_db.get_listing("a")["last_seen"]
        time.sleep(0.01)  # 确保 ISO 时间戳不同
        temp_db.diff([_l("a", status="X")])
        second_seen = temp_db.get_listing("a")["last_seen"]
        assert second_seen > first_seen

    def test_price_change_updates_record_but_no_event(self, temp_db):
        """price 变了但 status 没变 → 不产生 status_change 事件，但记录更新。"""
        temp_db.diff([_l("a", price_raw="€700")])
        new, changes = temp_db.diff([_l("a", price_raw="€800")])
        assert new == []
        assert changes == []
        # 但库里 price 应该更新
        assert temp_db.get_listing("a")["price_raw"] == "€800"


class TestDiffMixed:
    """新 + 变更 + 不变混合 batch。"""

    def test_mixed_batch(self, temp_db):
        # 第一轮：a 入库
        temp_db.diff([_l("a", status="X")])
        # 第二轮：a 变状态，b 新增，c 也新增
        new, changes = temp_db.diff([
            _l("a", status="Y"),
            _l("b"),
            _l("c"),
        ])
        assert {l.id for l in new} == {"b", "c"}
        assert len(changes) == 1
        assert changes[0][0].id == "a"

    def test_removed_listings_stay_in_db(self, temp_db):
        """本次 fresh 没出现的房源不被删除（保留历史记录）。"""
        temp_db.diff([_l("a"), _l("b"), _l("c")])
        # 第二轮只看到 a
        temp_db.diff([_l("a")])
        # b、c 应该仍在库
        assert temp_db.count_all() == 3
        assert temp_db.get_listing("b") is not None


# ─── 幂等性 & 原子性 ─────────────────────────────────────────────


class TestDiffIdempotency:
    """同一批 fresh 多次调用，状态收敛 —— 不会产生重复事件。"""

    def test_same_batch_twice(self, temp_db):
        fresh = [_l("a"), _l("b")]
        new1, ch1 = temp_db.diff(fresh)
        new2, ch2 = temp_db.diff(fresh)
        assert len(new1) == 2
        assert len(new2) == 0
        assert ch1 == ch2 == []

    def test_status_change_only_once_per_change(self, temp_db):
        """status A→B 之后再调用相同 fresh 不应该再次产出 status_change。"""
        temp_db.diff([_l("a", status="A")])
        new1, ch1 = temp_db.diff([_l("a", status="B")])
        assert len(ch1) == 1
        # 再次：还是 status=B
        new2, ch2 = temp_db.diff([_l("a", status="B")])
        assert new2 == []
        assert ch2 == []
        # status_changes 表里应该只有 1 条
        all_changes = temp_db.get_recent_changes(hours=24)
        assert len(all_changes) == 1


# ─── 通知标记 ────────────────────────────────────────────────────


class TestMarkNotified:
    def test_mark_notified_flips_flag(self, temp_db):
        temp_db.diff([_l("a")])
        assert temp_db.get_listing("a")["notified"] == 0
        temp_db.mark_notified("a")
        assert temp_db.get_listing("a")["notified"] == 1

    def test_mark_notified_batch(self, temp_db):
        temp_db.diff([_l("a"), _l("b"), _l("c")])
        temp_db.mark_notified_batch(["a", "c"])
        assert temp_db.get_listing("a")["notified"] == 1
        assert temp_db.get_listing("b")["notified"] == 0
        assert temp_db.get_listing("c")["notified"] == 1

    def test_mark_notified_batch_empty_noop(self, temp_db):
        temp_db.mark_notified_batch([])
        # 应该不抛异常

    def test_mark_status_change_notified(self, temp_db):
        temp_db.diff([_l("a", status="X")])
        temp_db.diff([_l("a", status="Y")])
        # status_changes 表里现在有一条未 notified
        temp_db.mark_status_change_notified("a")
        # 再次调用应该幂等（没有未 notified 的就不动）
        temp_db.mark_status_change_notified("a")


# ─── retry_queue 持久化 ────────────────────────────────────────


class TestRetryQueuePersistence:
    """竞败重试队列存到 meta 表，进程重启后能恢复。"""

    def test_empty_queue_returns_empty_dict(self, temp_db):
        assert temp_db.load_retry_queue() == {}

    def test_save_then_load_roundtrip(self, temp_db):
        original = {
            "user-a": {"listing-1", "listing-2"},
            "user-b": {"listing-3"},
        }
        temp_db.save_retry_queue(original)
        loaded = temp_db.load_retry_queue()
        assert loaded == original
        # 类型也要对
        assert isinstance(loaded["user-a"], set)

    def test_save_empty_clears_meta(self, temp_db):
        # 先存一份
        temp_db.save_retry_queue({"user-a": {"x"}})
        assert temp_db.load_retry_queue() != {}
        # 再清空
        temp_db.save_retry_queue({})
        assert temp_db.load_retry_queue() == {}

    def test_corrupted_json_returns_empty_and_clears(self, temp_db):
        """meta 表手工损坏 → 不抛异常，返回 {} 并自动清理。"""
        temp_db.set_meta("retry_queue", "{not valid json")
        result = temp_db.load_retry_queue()
        assert result == {}
        assert temp_db.get_meta("retry_queue", "DEFAULT") in ("", "DEFAULT")

    def test_non_list_value_skipped_with_warning(self, temp_db, caplog):
        """手动写入字符串/数字作为 user_id 的值 → 跳过并 warning。"""
        import json, logging
        temp_db.set_meta("retry_queue", json.dumps({"u1": "not_a_list", "u2": ["L1"]}))
        with caplog.at_level(logging.WARNING):
            result = temp_db.load_retry_queue()
        assert result == {"u2": {"L1"}}  # u1 被跳过
        assert "u1" in caplog.text

    def test_top_level_array_resets(self, temp_db, caplog):
        """顶层是 [] → reset + return {}."""
        import json, logging
        temp_db.set_meta("retry_queue", json.dumps([1, 2, 3]))
        with caplog.at_level(logging.WARNING):
            result = temp_db.load_retry_queue()
        assert result == {}
        assert "顶层" in caplog.text

    def test_top_level_string_resets(self, temp_db, caplog):
        """顶层是 "abc" → reset + return {}."""
        import logging
        temp_db.set_meta("retry_queue", '"just a string"')
        with caplog.at_level(logging.WARNING):
            result = temp_db.load_retry_queue()
        assert result == {}
        assert "顶层" in caplog.text


# ─── meta 表基础 ────────────────────────────────────────────────


class TestMeta:
    def test_get_default_on_missing(self, temp_db):
        assert temp_db.get_meta("nonexistent") == "—"
        assert temp_db.get_meta("nonexistent", default="ZZ") == "ZZ"

    def test_set_get_roundtrip(self, temp_db):
        temp_db.set_meta("k", "v1")
        assert temp_db.get_meta("k") == "v1"

    def test_set_overwrites(self, temp_db):
        temp_db.set_meta("k", "v1")
        temp_db.set_meta("k", "v2")
        assert temp_db.get_meta("k") == "v2"


# ─── reset_all ───────────────────────────────────────────────────


class TestResetAll:
    def test_reset_clears_all_tables(self, temp_db):
        temp_db.diff([_l("a", status="X")])
        temp_db.diff([_l("a", status="Y")])  # 产生 status_change
        temp_db.set_meta("foo", "bar")
        temp_db.add_web_notification(type="new_listing", title="t", body="b")

        assert temp_db.count_all() > 0
        assert len(temp_db.get_recent_changes(hours=24)) > 0
        assert temp_db.count_unread_notifications() > 0
        assert temp_db.get_meta("foo") == "bar"

        temp_db.reset_all()

        assert temp_db.count_all() == 0
        assert temp_db.get_recent_changes(hours=24) == []
        assert temp_db.count_unread_notifications() == 0
        assert temp_db.get_meta("foo", default="GONE") == "GONE"
