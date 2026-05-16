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
        self._conn.execute("DELETE FROM user_configs")
        placeholders = ", ".join("?" for _ in USER_CONFIG_COLUMNS)
        sql = (
            f"INSERT INTO user_configs ({', '.join(USER_CONFIG_COLUMNS)}) "
            f"VALUES ({placeholders})"
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

    @staticmethod
    def dumps_json(value) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def is_unique_violation(exc: BaseException) -> bool:
        return isinstance(exc, sqlite3.IntegrityError)
