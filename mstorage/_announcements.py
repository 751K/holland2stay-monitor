"""公告与逐用户投递记录。

为什么需要这张表
----------------
公告是「一次性群发」，但投递本身不是原子的：外部渠道、APNs、FCM 各自独立，
中途可能有个别用户失败。此前这些结果只存在于内存里的 ``AnnouncementResult``
和日志中，请求返回之后就没了——2026-08-28 FCM 全挂那次，事后无法回答
「谁收到了、谁需要补发」，只能整批重发，给已收到的人再打扰一遍。

现在的语义：
- ``announcements`` 一行 = 一次群发（内容 + 发送时的目标人数）；
- ``announcement_deliveries`` 一行 = 一个用户的一个渠道；
- ``failed`` / ``pending`` 可以被精确重试，``skipped`` 表示按设计不投递（如
  未配置外部渠道、没有设备、通知已关闭），重试会跳过它们。``pending`` 还有
  一层用途：群发开始前先给每个目标渠道写一行，进程中途被杀时重试仍知道
  该发给谁。
"""
from __future__ import annotations

from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AnnouncementOps:
    """依赖 self._conn。"""

    def create_announcement(
        self,
        title: str,
        body: str,
        *,
        recipients: int = 0,
        skipped_disabled: int = 0,
    ) -> int:
        with self._conn:
            cur = self._conn.execute(
                """INSERT INTO announcements
                       (title, body, created_at, recipients, skipped_disabled)
                   VALUES (?, ?, ?, ?, ?)""",
                (title, body, _now_iso(), int(recipients), int(skipped_disabled)),
            )
            return int(cur.lastrowid)  # type: ignore[arg-type]

    def get_announcement(self, announcement_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM announcements WHERE id = ?",
            (int(announcement_id),),
        ).fetchone()
        return dict(row) if row else None

    def list_announcements(self, limit: int = 20) -> list[dict]:
        """最近的公告，附带投递统计。

        统计口径故意按**用户**而不是按渠道行：面板上要回答的是「还有几个人
        没收到」，不是「失败了几次」。``push_devices`` 是成功设备数。
        """
        rows = self._conn.execute(
            """SELECT a.id, a.title, a.body, a.created_at,
                      a.recipients, a.skipped_disabled,
                      COUNT(DISTINCT CASE WHEN d.status='failed'
                                          THEN d.user_id END) AS failed_users,
                      COUNT(DISTINCT CASE WHEN d.status='pending'
                                          THEN d.user_id END) AS pending_users,
                      COUNT(DISTINCT CASE WHEN d.channel='external'
                                           AND d.status='ok'
                                          THEN d.user_id END) AS external_ok,
                      COALESCE(SUM(CASE WHEN d.channel='push'
                                        THEN d.devices ELSE 0 END), 0) AS push_devices
                 FROM announcements a
                 LEFT JOIN announcement_deliveries d
                        ON d.announcement_id = a.id
                GROUP BY a.id
                ORDER BY a.id DESC
                LIMIT ?""",
            (max(1, int(limit)),),
        ).fetchall()
        return [dict(r) for r in rows]

    def seed_announcement_deliveries(
        self,
        announcement_id: int,
        user_ids: list[str],
        channels: tuple[str, ...],
    ) -> None:
        """群发前把所有「答应要投」的行一次写成 pending。

        一批写完而不是逐行提交：进程在写入过程中被杀时，要么整批存在、要么
        整批不存在；半批会让「谁还没投递」这个问题变得无解。
        """
        if not user_ids:
            return
        now = _now_iso()
        rows = [
            (int(announcement_id), user_id, channel, "pending", 0, "", now)
            for user_id in user_ids
            for channel in channels
        ]
        with self._conn:
            self._conn.executemany(
                """INSERT INTO announcement_deliveries
                       (announcement_id, user_id, channel, status,
                        devices, error, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(announcement_id, user_id, channel) DO UPDATE SET
                       status=excluded.status,
                       devices=0,
                       error='',
                       updated_at=excluded.updated_at""",
                rows,
            )

    def upsert_announcement_delivery(
        self,
        announcement_id: int,
        user_id: str,
        channel: str,
        status: str,
        *,
        devices: int = 0,
        error: str = "",
    ) -> None:
        """写入/覆盖一个用户在一个渠道上的投递结果。

        幂等：重试成功后同一行从 failed 变成 ok，不会留下两份互相矛盾的记录。
        """
        with self._conn:
            self._conn.execute(
                """INSERT INTO announcement_deliveries
                       (announcement_id, user_id, channel, status,
                        devices, error, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(announcement_id, user_id, channel) DO UPDATE SET
                       status=excluded.status,
                       devices=excluded.devices,
                       error=excluded.error,
                       updated_at=excluded.updated_at""",
                (
                    int(announcement_id), user_id, channel, status,
                    int(devices), (error or "")[:500], _now_iso(),
                ),
            )

    def list_announcement_deliveries(
        self,
        announcement_id: int,
        *,
        status: str | None = None,
    ) -> list[dict]:
        sql = (
            "SELECT * FROM announcement_deliveries WHERE announcement_id = ?"
        )
        args: list = [int(announcement_id)]
        if status is not None:
            sql += " AND status = ?"
            args.append(status)
        sql += " ORDER BY user_id, channel"
        return [dict(r) for r in self._conn.execute(sql, args).fetchall()]
