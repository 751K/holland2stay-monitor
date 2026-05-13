"""Web 通知的写入、查询、标记已读、清理。"""

from __future__ import annotations


class NotificationOps:
    """依赖 self._conn。"""

    def add_web_notification(
        self,
        *,
        type: str,
        title: str,
        body: str = "",
        url: str = "",
        listing_id: str = "",
    ) -> int:
        with self._conn:
            cur = self._conn.execute(
                """INSERT INTO web_notifications (type, title, body, url, listing_id)
                   VALUES (?, ?, ?, ?, ?)""",
                (type, title, body, url, listing_id),
            )
            return cur.lastrowid  # type: ignore[return-value]

    def get_notifications(self, limit: int = 50, offset: int = 0) -> list[dict]:
        rows = self._conn.execute(
            """SELECT * FROM web_notifications
               ORDER BY created_at DESC LIMIT ? OFFSET ?""",
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_notifications_since(self, last_id: int) -> list[dict]:
        rows = self._conn.execute(
            """SELECT * FROM web_notifications
               WHERE id > ? ORDER BY id ASC""",
            (last_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def count_unread_notifications(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM web_notifications WHERE read=0"
        ).fetchone()
        return row[0] if row else 0

    def mark_notifications_read(self, ids: list[int] | None = None) -> None:
        if ids is not None and not ids:
            return
        with self._conn:
            if ids is None:
                self._conn.execute(
                    "UPDATE web_notifications SET read=1 WHERE read=0"
                )
            else:
                placeholders = ",".join("?" * len(ids))
                self._conn.execute(
                    f"UPDATE web_notifications SET read=1 WHERE id IN ({placeholders})",
                    ids,
                )

    def prune_notifications(self, keep: int = 500) -> int:
        with self._conn:
            cur = self._conn.execute(
                """DELETE FROM web_notifications
                   WHERE id NOT IN (
                       SELECT id FROM web_notifications ORDER BY id DESC LIMIT ?
                   )""",
                (keep,),
            )
            return cur.rowcount
