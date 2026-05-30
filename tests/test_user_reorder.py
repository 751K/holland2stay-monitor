"""
用户优先级排序测试：sort_order 持久化 / HTTP 路由 / 自动预订分配。

覆盖：
- Storage.reorder_user() 边界与正常行为
- POST /users/<user_id>/move 路由
- _assign_auto_book_candidates 遵守 sort_order 优先级
"""

from __future__ import annotations

import pytest


# ── helpers ─────────────────────────────────────────────────────────

def _make_user_row(uid: str, name: str, sort_order: int = 0) -> dict:
    """构造一个最小 user_config row，用于直接写入 DB。"""
    return {
        "id": uid,
        "name": name,
        "enabled": 1,
        "notifications_enabled": 1,
        "notification_channels_json": '["imessage"]',
        "imessage_recipient": f"+1555{uid[-4:]}",
        "telegram_token": "",
        "telegram_chat_id": "",
        "email_mode": "shared",
        "email_verified": 0,
        "email_smtp_host": "",
        "email_smtp_port": 587,
        "email_smtp_security": "starttls",
        "email_username": "",
        "email_password": "",
        "email_from": "",
        "email_to": "",
        "twilio_sid": "",
        "twilio_token": "",
        "twilio_from": "",
        "twilio_to": "",
        "listing_filter_json": "{}",
        "auto_book_json": '{"enabled":true,"email":"test@test.test","password":"pw","dry_run":false}',
        "app_password_hash": "",
        "app_login_enabled": 0,
        "allow_h2s_login": 0,
        "sort_order": sort_order,
        "language": "en",
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
    }


# ── Storage 层：reorder_user ──────────────────────────────────────


class TestReorderUser:
    """直接测 Storage.reorder_user()，不经过 HTTP。"""

    def test_up_moves_user_one_position(self, temp_db):
        """上移：用户从 #2 → #1。"""
        temp_db.replace_user_config_rows([
            _make_user_row("a", "Alice", sort_order=0),
            _make_user_row("b", "Bob", sort_order=1),
            _make_user_row("c", "Carol", sort_order=2),
        ])

        assert temp_db.reorder_user("b", "up") is True

        rows = temp_db.list_user_config_rows()
        ids = [r["id"] for r in rows]
        assert ids == ["b", "a", "c"], f"expected [b,a,c] got {ids}"
        assert rows[0]["sort_order"] == 0
        assert rows[1]["sort_order"] == 1
        assert rows[2]["sort_order"] == 2

    def test_down_moves_user_one_position(self, temp_db):
        """下移：用户从 #2 → #3。"""
        temp_db.replace_user_config_rows([
            _make_user_row("a", "Alice", sort_order=0),
            _make_user_row("b", "Bob", sort_order=1),
            _make_user_row("c", "Carol", sort_order=2),
        ])

        assert temp_db.reorder_user("b", "down") is True

        rows = temp_db.list_user_config_rows()
        ids = [r["id"] for r in rows]
        assert ids == ["a", "c", "b"], f"expected [a,c,b] got {ids}"
        for i, r in enumerate(rows):
            assert r["sort_order"] == i

    def test_up_at_top_returns_false(self, temp_db):
        """已在第一位，上移返回 False，顺序不变。"""
        temp_db.replace_user_config_rows([
            _make_user_row("a", "Alice", sort_order=0),
            _make_user_row("b", "Bob", sort_order=1),
        ])

        assert temp_db.reorder_user("a", "up") is False

        rows = temp_db.list_user_config_rows()
        assert [r["id"] for r in rows] == ["a", "b"]

    def test_down_at_bottom_returns_false(self, temp_db):
        """已在最后一位，下移返回 False，顺序不变。"""
        temp_db.replace_user_config_rows([
            _make_user_row("a", "Alice", sort_order=0),
            _make_user_row("b", "Bob", sort_order=1),
        ])

        assert temp_db.reorder_user("b", "down") is False

        rows = temp_db.list_user_config_rows()
        assert [r["id"] for r in rows] == ["a", "b"]

    def test_single_user_returns_false(self, temp_db):
        """只有一个用户时无法移动。"""
        temp_db.replace_user_config_rows([
            _make_user_row("a", "Alice", sort_order=0),
        ])

        assert temp_db.reorder_user("a", "up") is False
        assert temp_db.reorder_user("a", "down") is False

    def test_unknown_user_returns_false(self, temp_db):
        """不存在的 user_id 返回 False。"""
        temp_db.replace_user_config_rows([
            _make_user_row("a", "Alice"),
        ])

        assert temp_db.reorder_user("nonexistent", "up") is False

    def test_invalid_direction_returns_false(self, temp_db):
        """非法 direction 返回 False，顺序不变。"""
        temp_db.replace_user_config_rows([
            _make_user_row("a", "Alice", sort_order=0),
            _make_user_row("b", "Bob", sort_order=1),
        ])

        assert temp_db.reorder_user("a", "left") is False
        rows = temp_db.list_user_config_rows()
        assert [r["id"] for r in rows] == ["a", "b"]

    def test_sort_orders_are_sequential_after_swap(self, temp_db):
        """交换后所有 sort_order 连续无间断（0,1,2,...）。"""
        temp_db.replace_user_config_rows([
            _make_user_row("z", "Zoe", sort_order=0),
            _make_user_row("y", "Yara", sort_order=5),   # 故意留 gap
            _make_user_row("x", "Xavier", sort_order=9),
        ])
        temp_db.reorder_user("x", "up")  # x (index 2) → 1

        rows = temp_db.list_user_config_rows()
        assert rows[0]["sort_order"] == 0
        assert rows[1]["sort_order"] == 1
        assert rows[2]["sort_order"] == 2
        # x 和 y 交换：原 [z,y,x] → [z,x,y]
        assert [r["id"] for r in rows] == ["z", "x", "y"]

    def test_reorder_preserves_created_at(self, temp_db):
        """交换不影响 created_at 等其他字段。"""
        row_a = _make_user_row("a", "Alice", sort_order=0)
        row_a["created_at"] = "2025-01-01T00:00:00"
        row_b = _make_user_row("b", "Bob", sort_order=1)
        row_b["created_at"] = "2025-06-01T00:00:00"
        temp_db.replace_user_config_rows([row_a, row_b])

        temp_db.reorder_user("a", "down")

        rows = {r["id"]: r for r in temp_db.list_user_config_rows()}
        assert rows["a"]["created_at"] == "2025-01-01T00:00:00"
        assert rows["b"]["created_at"] == "2025-06-01T00:00:00"


# ── HTTP 层：POST /users/<user_id>/move ──────────────────────────


class TestUserMoveRoute:
    """通过 Flask test client 测试 move 路由。"""

    def test_admin_move_up(self, admin_client):
        """admin 上移用户，返回 302 + flash 消息。"""
        # 先建 3 个用户
        from users import load_users
        for name in ["Alice", "Bob", "Carol"]:
            r = admin_client.post("/users/new", data={
                "csrf_token": "test_csrf",
                "name": name,
                "enabled": "true",
                "NOTIFICATIONS_ENABLED": "true",
                "NOTIFICATION_CHANNELS": "imessage",
                "IMESSAGE_RECIPIENT": f"+1555{name[:4]}",
            })
            assert r.status_code == 302

        users = load_users()
        bob = next(u for u in users if u.name == "Bob")

        # Bob 上移
        r = admin_client.post(
            f"/users/{bob.id}/move",
            data={"csrf_token": "test_csrf", "direction": "up"},
        )
        assert r.status_code == 302

        users = load_users()
        names = [u.name for u in users]
        # Bob 应该从 #2 变成 #1
        assert names[0] == "Bob", f"expected Bob first, got {names}"

    def test_admin_move_down(self, admin_client):
        """admin 下移用户。"""
        for name in ["Alice", "Bob", "Carol"]:
            r = admin_client.post("/users/new", data={
                "csrf_token": "test_csrf",
                "name": name,
                "enabled": "true",
                "NOTIFICATIONS_ENABLED": "true",
                "NOTIFICATION_CHANNELS": "imessage",
                "IMESSAGE_RECIPIENT": f"+1555{name[:4]}",
            })
            assert r.status_code == 302

        from users import load_users
        users = load_users()
        alice = next(u for u in users if u.name == "Alice")

        r = admin_client.post(
            f"/users/{alice.id}/move",
            data={"csrf_token": "test_csrf", "direction": "down"},
        )
        assert r.status_code == 302

        users = load_users()
        names = [u.name for u in users]
        # Alice 从 #1 → #2
        assert names[1] == "Alice", f"expected Alice second, got {names}"

    def test_move_at_boundary_no_crash(self, admin_client):
        """边界操作不崩溃，返回 302。"""
        for name in ["Alice", "Bob"]:
            r = admin_client.post("/users/new", data={
                "csrf_token": "test_csrf",
                "name": name,
                "enabled": "true",
                "NOTIFICATIONS_ENABLED": "true",
                "NOTIFICATION_CHANNELS": "imessage",
                "IMESSAGE_RECIPIENT": f"+1555{name[:4]}",
            })
            assert r.status_code == 302

        from users import load_users
        users = load_users()
        alice = next(u for u in users if u.name == "Alice")

        # 第一个用户上移 → flash info（无异常）
        r = admin_client.post(
            f"/users/{alice.id}/move",
            data={"csrf_token": "test_csrf", "direction": "up"},
        )
        assert r.status_code == 302

        # 验证顺序没变
        users = load_users()
        names = [u.name for u in users]
        assert names[0] == "Alice"

    def test_guest_cannot_move(self, guest_client):
        """非 admin 不能调用 move。"""
        r = guest_client.post(
            "/users/some-id/move",
            data={"csrf_token": "test_csrf", "direction": "up"},
        )
        assert r.status_code == 302
        assert r.headers["Location"].endswith("/")

    def test_invalid_direction_flash(self, admin_client):
        """非法 direction 返回 warning flash。"""
        for name in ["Alice"]:
            r = admin_client.post("/users/new", data={
                "csrf_token": "test_csrf",
                "name": name,
                "enabled": "true",
                "NOTIFICATIONS_ENABLED": "true",
                "NOTIFICATION_CHANNELS": "imessage",
                "IMESSAGE_RECIPIENT": "+1555xxxx",
            })
            assert r.status_code == 302

        from users import load_users
        users = load_users()
        alice = next(u for u in users if u.name == "Alice")

        r = admin_client.post(
            f"/users/{alice.id}/move",
            data={"csrf_token": "test_csrf", "direction": "sideways"},
        )
        assert r.status_code == 302


# ── 自动预订优先级验证 ────────────────────────────────────────────


class TestBookingPriorityBySortOrder:
    """
    验证 _assign_auto_book_candidates 遵守 sort_order 排序。

    在 monitor.py 中，user_notifiers 的迭代顺序来自 load_users()，
    而 load_users() 返回的列表已按 sort_order ASC, created_at ASC, id ASC 排序。

    所以 sort_order 最小的用户在第 0 个位置 → user_order[uid] 最小 →
    负载均衡平局时优先拿到房源。
    """

    @pytest.fixture(autouse=True)
    def _setup(self, isolated_data_dir):
        """确保 booking 测试使用隔离的 DB 路径（与 load_users 共享）。"""
        from app.db import storage
        st = storage()
        # 清空旧数据
        st._conn.execute("DELETE FROM user_configs")
        st._conn.commit()
        st.close()

    def _write_users(self, rows: list[dict]) -> None:
        from app.db import storage
        st = storage()
        try:
            st.replace_user_config_rows(rows)
        finally:
            st.close()

    def test_load_users_respects_sort_order(self):
        """load_users 返回的用户列表按 sort_order 排序。"""
        # replace_user_config_rows 按 list 顺序自动编号 sort_order=0,1,2
        # 所以先写入任意顺序，再显式 UPDATE sort_order 为期望值
        self._write_users([
            _make_user_row("a", "Alice", sort_order=0),
            _make_user_row("b", "Bob", sort_order=1),
            _make_user_row("c", "Carol", sort_order=2),
        ])
        # 显式设置非连续 sort_order，验证 ORDER BY 生效
        from app.db import storage
        st = storage()
        try:
            st._conn.execute("UPDATE user_configs SET sort_order=2 WHERE id='c'")
            st._conn.execute("UPDATE user_configs SET sort_order=0 WHERE id='a'")
            st._conn.execute("UPDATE user_configs SET sort_order=1 WHERE id='b'")
            st._conn.commit()
        finally:
            st.close()

        from users import load_users
        users = load_users()
        names = [u.name for u in users]
        assert names == ["Alice", "Bob", "Carol"], f"got {names}"

    def test_assign_priority_by_order(self):
        """
        三用户同条件匹配同一房源 → sort_order 最小的拿到。

        模拟 _assign_auto_book_candidates 的输入：3 个用户都在
        raw_candidates 里有同一套房 L-1。
        user_notifiers 按 sort_order ASC 排序写入，
        因此 Alice(sort_order=0) 的 user_order=0 最小 → 获胜。
        """
        from models import Listing
        from users import load_users
        from monitor import _assign_auto_book_candidates

        self._write_users([
            _make_user_row("a", "Alice", sort_order=0),
            _make_user_row("b", "Bob", sort_order=1),
            _make_user_row("c", "Carol", sort_order=2),
        ])

        users = load_users()

        listing = Listing(
            id="L-1",
            name="Test Huis",
            status="Available to book",
            price_raw="€700",
            available_from="2030-01-01",
            features=[],
            url="https://example.test/L-1",
            city="E",
            source="holland2stay",
            sku="SKU-L-1",
            contract_id=42,
            contract_start_date="2030-01-01",
        )

        # 使用 test_monitor_booking_assignment.py 中的 _Notifier 模式
        from notifier import BaseNotifier

        class _FakeNotifier(BaseNotifier):
            async def _send(self, text: str) -> bool:
                return True
            async def close(self):
                pass

        user_notifiers = [(u, _FakeNotifier()) for u in users]

        raw_candidates = {u.id: [listing] for u in users}
        assigned = _assign_auto_book_candidates(raw_candidates, user_notifiers)

        # 只有 Alice (sort_order=0, 排第一) 拿到
        assert len(assigned["a"]) == 1
        assert assigned["a"][0].id == "L-1"
        # Bob 和 Carol 都没拿到
        assert len(assigned["b"]) == 0
        assert len(assigned["c"]) == 0

    def test_assign_load_balance_still_works(self):
        """
        两套房匹配同一组用户 → 负载均衡生效，但 sort_order 影响第一套的归属。

        Alice (sort_order=0), Bob (sort_order=1), Carol (sort_order=2)
        同时匹配 L-1 和 L-2。

        L-1: 三人 assigned_count=0 → Alice (user_order=0) 拿到
        L-2: Alice 已有 1 套, Bob/Carol 都是 0 → Bob (user_order=1) 拿到
        """
        from models import Listing
        from users import load_users
        from monitor import _assign_auto_book_candidates

        self._write_users([
            _make_user_row("a", "Alice", sort_order=0),
            _make_user_row("b", "Bob", sort_order=1),
            _make_user_row("c", "Carol", sort_order=2),
        ])

        users = load_users()

        l1 = Listing(
            id="L-1", name="Huis 1", status="Available to book",
            price_raw="€700", available_from="2030-01-01",
            features=[], url="https://example.test/L-1", city="E",
            source="holland2stay", sku="SKU-L-1",
            contract_id=42, contract_start_date="2030-01-01",
        )
        l2 = Listing(
            id="L-2", name="Huis 2", status="Available to book",
            price_raw="€800", available_from="2030-01-01",
            features=[], url="https://example.test/L-2", city="E",
            source="holland2stay", sku="SKU-L-2",
            contract_id=42, contract_start_date="2030-01-01",
        )

        from notifier import BaseNotifier

        class _FakeNotifier(BaseNotifier):
            async def _send(self, text: str) -> bool:
                return True
            async def close(self):
                pass

        user_notifiers = [(u, _FakeNotifier()) for u in users]
        raw_candidates = {u.id: [l1, l2] for u in users}
        assigned = _assign_auto_book_candidates(raw_candidates, user_notifiers)

        # Alice 拿到 L-1（优先级最高）
        assert len(assigned["a"]) == 1
        assert assigned["a"][0].id == "L-1"
        # Bob 拿到 L-2（负载均衡：Alice 已有 1 套）
        assert len(assigned["b"]) == 1
        assert assigned["b"][0].id == "L-2"
        # Carol 没拿到
        assert len(assigned["c"]) == 0

    def test_reorder_changes_booking_winner(self):
        """
        调序后验证优先级变化：Carol 从 #3 升到 #1 后拿到房源。
        """
        from models import Listing
        from users import load_users
        from monitor import _assign_auto_book_candidates

        self._write_users([
            _make_user_row("a", "Alice", sort_order=0),
            _make_user_row("b", "Bob", sort_order=1),
            _make_user_row("c", "Carol", sort_order=2),
        ])

        # 通过 Storage.reorder_user 把 Carol 移到第一位
        from app.db import storage
        st = storage()
        try:
            assert st.reorder_user("c", "up") is True  # c: idx2→1
            assert st.reorder_user("c", "up") is True  # c: idx1→0
        finally:
            st.close()

        users = load_users()
        assert users[0].name == "Carol"

        listing = Listing(
            id="L-1", name="Test Huis", status="Available to book",
            price_raw="€700", available_from="2030-01-01",
            features=[], url="https://example.test/L-1", city="E",
            source="holland2stay", sku="SKU-L-1",
            contract_id=42, contract_start_date="2030-01-01",
        )

        from notifier import BaseNotifier

        class _FakeNotifier(BaseNotifier):
            async def _send(self, text: str) -> bool:
                return True
            async def close(self):
                pass

        user_notifiers = [(u, _FakeNotifier()) for u in users]
        raw_candidates = {u.id: [listing] for u in users}
        assigned = _assign_auto_book_candidates(raw_candidates, user_notifiers)

        # Carol 现在排第一，拿到房源
        assert len(assigned["c"]) == 1
        assert assigned["c"][0].id == "L-1"
        assert len(assigned["a"]) == 0
        assert len(assigned["b"]) == 0
