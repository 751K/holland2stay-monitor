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
            f"INSERT INTO user_configs ({', '.join(USER_CONFIG_COLUMNS)}) VALUES ({placeholders}) "
            "ON CONFLICT(id) DO UPDATE SET "
            "name=excluded.name, "
            "enabled=excluded.enabled, "
            "notifications_enabled=excluded.notifications_enabled, "
            "notification_channels_json=excluded.notification_channels_json, "
            "imessage_recipient=excluded.imessage_recipient, "
            "telegram_token=excluded.telegram_token, "
            "telegram_chat_id=excluded.telegram_chat_id, "
            "email_mode=excluded.email_mode, "
            "email_verified=excluded.email_verified, "
            "email_smtp_host=excluded.email_smtp_host, "
            "email_smtp_port=excluded.email_smtp_port, "
            "email_smtp_security=excluded.email_smtp_security, "
            "email_username=excluded.email_username, "
            "email_password=excluded.email_password, "
            "email_from=excluded.email_from, "
            "email_to=excluded.email_to, "
            "twilio_sid=excluded.twilio_sid, "
            "twilio_token=excluded.twilio_token, "
            "twilio_from=excluded.twilio_from, "
            "twilio_to=excluded.twilio_to, "
            "listing_filter_json=excluded.listing_filter_json, "
            "auto_book_json=excluded.auto_book_json, "
            "app_password_hash=excluded.app_password_hash, "
            "app_login_enabled=excluded.app_login_enabled, "
            "allow_h2s_login=excluded.allow_h2s_login, "
            "sort_order=excluded.sort_order, "
            "updated_at=excluded.updated_at"
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

    @staticmethod
    def dumps_json(value) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def is_unique_violation(exc: BaseException) -> bool:
        return isinstance(exc, sqlite3.IntegrityError)
