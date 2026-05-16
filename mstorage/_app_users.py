"""App 用户账号表（注册/登录身份），与 users.json 配置分离。"""

from __future__ import annotations

import sqlite3
from typing import Optional

from mstorage._tokens import _utc_now_iso


class AppUserOps:
    """依赖 self._conn（由 StorageBase 提供）。"""

    def create_app_user(
        self,
        *,
        user_id: str,
        name: str,
        enabled: bool = True,
        app_login_enabled: bool = True,
        app_password_hash: str = "",
    ) -> dict:
        """
        创建 App 用户账号。name 由 SQLite UNIQUE 约束保证并发唯一。

        Raises
        ------
        sqlite3.IntegrityError
            user_id 或 name 已存在。
        """
        now = _utc_now_iso()
        with self._conn:
            self._conn.execute(
                """INSERT INTO app_users
                       (id, name, enabled, app_login_enabled,
                        app_password_hash, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    user_id,
                    name,
                    1 if enabled else 0,
                    1 if app_login_enabled else 0,
                    app_password_hash,
                    now,
                    now,
                ),
            )
        row = self.get_app_user(user_id)
        if row is None:  # pragma: no cover - SQLite insert succeeded but row vanished
            raise RuntimeError("app user create succeeded but row is missing")
        return row

    def upsert_app_user(
        self,
        *,
        user_id: str,
        name: str,
        enabled: bool,
        app_login_enabled: bool,
        app_password_hash: str,
    ) -> dict:
        """从 users.json 配置同步/迁移账号字段到 SQLite。"""
        now = _utc_now_iso()
        params = (
            user_id,
            name,
            1 if enabled else 0,
            1 if app_login_enabled else 0,
            app_password_hash,
            now,
            now,
        )
        sql = """INSERT INTO app_users
                    (id, name, enabled, app_login_enabled,
                    app_password_hash, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    enabled = excluded.enabled,
                    app_login_enabled = excluded.app_login_enabled,
                    app_password_hash = excluded.app_password_hash,
                    updated_at = excluded.updated_at"""
        with self._conn:
            self._conn.execute(sql, params)
        row = self.get_app_user(user_id)
        if row is None:  # pragma: no cover
            raise RuntimeError("app user upsert succeeded but row is missing")
        return row

    def sync_app_user_from_config(self, user) -> dict:
        """用 UserConfig 镜像刷新 SQLite 账号表。"""
        return self.upsert_app_user(
            user_id=user.id,
            name=user.name,
            enabled=bool(user.enabled),
            app_login_enabled=bool(user.app_login_enabled),
            app_password_hash=user.app_password_hash or "",
        )

    def get_app_user(self, user_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM app_users WHERE id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_app_user_by_name(self, name: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM app_users WHERE name = ?",
            (name,),
        ).fetchone()
        return dict(row) if row else None

    def delete_app_user(self, user_id: str) -> bool:
        with self._conn:
            cur = self._conn.execute("DELETE FROM app_users WHERE id = ?", (user_id,))
            return cur.rowcount > 0

    @staticmethod
    def is_unique_violation(exc: BaseException) -> bool:
        return isinstance(exc, sqlite3.IntegrityError)
