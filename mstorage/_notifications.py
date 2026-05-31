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
        user_id: str = "",
    ) -> int:
        """
        写入一条 Web 通知。

        user_id : 默认 ""（系统级，所有 admin 可见）；
                  Phase 3 APNs 后会传入具体 UserConfig.id 用于 per-user 隔离。
                  目前所有现存写入路径都不传，保持 backward-compat。
        """
        with self._conn:
            cur = self._conn.execute(
                """INSERT INTO web_notifications
                       (type, title, body, url, listing_id, user_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (type, title, body, url, listing_id, user_id),
            )
            return cur.lastrowid  # type: ignore[return-value]

    def get_notifications(
        self,
        limit: int = 50,
        offset: int = 0,
        *,
        user_id: str | None = None,
        within_days: int | None = None,
    ) -> list[dict]:
        """
        分页查询 Web 通知。

        user_id=None  : 不按 user_id 过滤（admin 视角，看全部）
        user_id="x"   : 仅返回 user_id="x" 或 ""（系统通知）的行
        within_days=N : 只返回最近 N 天的通知（created_at 是 ISO UTC 文本
                        '...T...Z'，字符串可按时间序比较）。None = 不限时间。
                        用于把 App Alerts 工作集压小——旧通知太多时客户端
                        切类型筛选要 diff 上千行会卡。
        """
        conds: list[str] = []
        params: list = []
        if user_id is not None:
            conds.append("(user_id = ? OR user_id = '')")
            params.append(user_id)
        if within_days is not None:
            conds.append("created_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)")
            params.append(f"-{int(within_days)} days")
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        rows = self._conn.execute(
            f"SELECT * FROM web_notifications{where}"
            " ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_notifications_since(
        self,
        last_id: int,
        *,
        user_id: str | None = None,
    ) -> list[dict]:
        """SSE 用：返回 id > last_id 的增量；同 get_notifications 的 user_id 语义。"""
        if user_id is None:
            rows = self._conn.execute(
                """SELECT * FROM web_notifications
                   WHERE id > ? ORDER BY id ASC""",
                (last_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT * FROM web_notifications
                   WHERE id > ? AND (user_id = ? OR user_id = '')
                   ORDER BY id ASC""",
                (last_id, user_id),
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
