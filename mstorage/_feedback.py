"""用户反馈的写入。

表结构在 ``_base`` 的 schema 里创建；这里只负责插入，不负责建表。
"""

from __future__ import annotations


class FeedbackOps:
    """依赖 self._conn。"""

    def add_feedback(
        self,
        *,
        kind: str,
        message: str,
        role: str,
        user_id: str = "",
        user_name: str = "",
        app_version: str = "",
        platform: str = "ios",
    ) -> int:
        """写入一条用户反馈，返回自增 id。

        ``platform`` 目前由 API 路由忽略，保留默认值 ``ios``；调用方显式传入时
        才会写入其它值。
        """
        with self._conn:
            cur = self._conn.execute(
                """INSERT INTO feedback
                       (kind, message, role, user_id, user_name, app_version, platform)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (kind, message, role, user_id, user_name, app_version, platform),
            )
            return cur.lastrowid  # type: ignore[return-value]
