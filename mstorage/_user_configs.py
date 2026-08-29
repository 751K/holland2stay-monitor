"""UserConfig 的 SQLite 持久化。

本层只处理可序列化的 dict/JSON，不 import users.UserConfig，避免
storage -> users -> storage 的循环依赖。UserConfig dataclass 与加密/解密
仍由 users.py 负责。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Iterable

from mstorage._tokens import _utc_now_iso


USER_CONFIG_COLUMNS = (
    "id",
    "name",
    "enabled",
    "notifications_enabled",
    "notification_channels_json",
    "imessage_recipient",
    "telegram_token",
    "telegram_chat_id",
    "email_mode",
    "email_verified",
    "email_smtp_host",
    "email_smtp_port",
    "email_smtp_security",
    "email_username",
    "email_password",
    "email_from",
    "email_to",
    "twilio_sid",
    "twilio_token",
    "twilio_from",
    "twilio_to",
    "listing_filter_json",
    "auto_book_json",
    "app_password_hash",
    "app_login_enabled",
    "allow_h2s_login",
    "sort_order",
    "language",
    "created_at",
    "updated_at",
)


#: ``ON CONFLICT DO UPDATE`` 的 SET 子句，由列表推出而不是手写。
#:
#: 原先这串是逐列手打的，加列时漏掉 ``language``——于是它只在 INSERT 时写得进去，
#: 之后无论怎么保存都改不动，而且不会报错：新建用户选中文是对的，编辑时改成英文
#: 点了保存、提示「已保存」，值纹丝不动。2026-08-29 加「通知语言」入口时撞上。
#:
#: ``id`` 是冲突键，``created_at`` 必须保留首次写入的值，其余一律跟随 excluded。
_UPDATE_SET = ", ".join(
    f"{c}=excluded.{c}"
    for c in USER_CONFIG_COLUMNS
    if c not in ("id", "created_at")
)


class UserConfigOps:
    """依赖 self._conn（由 StorageBase 提供）。"""

    def list_user_config_rows(self) -> list[dict]:
        rows = self._conn.execute(
            """SELECT *
                 FROM user_configs
                ORDER BY sort_order ASC, created_at ASC, id ASC"""
        ).fetchall()
        return [dict(r) for r in rows]

    def count_user_configs(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM user_configs").fetchone()
        return int(row[0]) if row else 0

    def replace_user_config_rows(self, rows: Iterable[dict]) -> None:
        with self._conn:
            self.replace_user_config_rows_unlocked(rows)

    def replace_user_config_rows_unlocked(self, rows: Iterable[dict]) -> None:
        """
        用 rows 完整替换 user_configs。

        调用方如果已经持有 BEGIN IMMEDIATE 事务，应使用本 unlocked 版本；
        否则用 replace_user_config_rows()。
        """
        materialized = list(rows)
        now = _utc_now_iso()
        existing_created = {
            r["id"]: r["created_at"]
            for r in self._conn.execute(
                "SELECT id, created_at FROM user_configs"
            ).fetchall()
        }
        incoming_ids = {str(row["id"]) for row in materialized}
        if incoming_ids:
            placeholders = ", ".join("?" for _ in incoming_ids)
            self._conn.execute(
                f"DELETE FROM user_configs WHERE id NOT IN ({placeholders})",
                tuple(incoming_ids),
            )
        else:
            self._conn.execute("DELETE FROM user_configs")
        placeholders = ", ".join("?" for _ in USER_CONFIG_COLUMNS)
        sql = (
            f"INSERT INTO user_configs ({', '.join(USER_CONFIG_COLUMNS)}) "
            f"VALUES ({placeholders}) "
            "ON CONFLICT(id) DO UPDATE SET " + _UPDATE_SET
        )
        for idx, row in enumerate(materialized):
            item = dict(row)
            item["sort_order"] = idx
            item["created_at"] = item.get("created_at") or existing_created.get(item["id"]) or now
            item["updated_at"] = now
            self._conn.execute(sql, tuple(item.get(col, "") for col in USER_CONFIG_COLUMNS))

    def get_user_config_row_by_name(self, name: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM user_configs WHERE name = ?",
            (name,),
        ).fetchone()
        return dict(row) if row else None

    def get_user_config_row(self, user_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM user_configs WHERE id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None

    def reorder_user(self, user_id: str, direction: str) -> bool:
        """将 user_id 向上（direction='up'）或向下（direction='down'）移动一位。

        通过交换相邻用户的 sort_order 实现。所有用户按
        ``sort_order ASC, created_at ASC, id ASC`` 排序后重新编号，
        保证 sort_order 始终是连续的 0, 1, 2, ...。

        Returns
        -------
        bool  True 表示移动成功，False 表示已在边界无法移动。
        """
        rows = self.list_user_config_rows()
        if len(rows) < 2:
            return False

        idx = next((i for i, r in enumerate(rows) if str(r["id"]) == user_id), None)
        if idx is None:
            return False

        if direction == "up":
            if idx == 0:
                return False
            rows[idx], rows[idx - 1] = rows[idx - 1], rows[idx]
        elif direction == "down":
            if idx >= len(rows) - 1:
                return False
            rows[idx], rows[idx + 1] = rows[idx + 1], rows[idx]
        else:
            return False

        with self._conn:
            for new_idx, row in enumerate(rows):
                self._conn.execute(
                    "UPDATE user_configs SET sort_order = ? WHERE id = ?",
                    (new_idx, str(row["id"])),
                )
        return True

    def reorder_users_bulk(self, ordered_ids: list[str]) -> None:
        """批量更新 sort_order，按 ordered_ids 的顺序从 0 开始编号。"""
        with self._conn:
            for idx, uid in enumerate(ordered_ids):
                self._conn.execute(
                    "UPDATE user_configs SET sort_order = ? WHERE id = ?",
                    (idx, uid),
                )

    @staticmethod
    def dumps_json(value) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def is_unique_violation(exc: BaseException) -> bool:
        return isinstance(exc, sqlite3.IntegrityError)
