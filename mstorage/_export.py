"""GDPR 用户数据导出：一次取回通知历史、活跃设备与活跃会话。

导出语义与 API 的分页查询刻意不同，改动时不要直接复用筛选逻辑：

- 通知：该用户最近 500 条，不按时间窗口截断，也不做 ``listing_filter`` 二次过滤
  ——用户有权拿到系统里属于自己的全部记录。
- 设备：只导出未禁用（``disabled_at IS NULL``）的设备，并附带 ``os_version``。
- 令牌：只导出未撤销（``revoked = 0``）的会话，不返回 token 哈希或明文。

字段结构是对外契约，见 ``docs/API.md`` 的 ``GET /me/export``。
"""

from __future__ import annotations

#: 导出包含的通知条数上限，与 API 文档保持一致。
NOTIFICATION_EXPORT_LIMIT = 500


class ExportOps:
    """依赖 self._conn。"""

    def export_user_data(self, user_id: str) -> dict:
        """返回 ``{notification_history, active_devices, active_tokens}``。"""
        notif_rows = self._conn.execute(
            "SELECT id, created_at, type, title, body, url, listing_id, read "
            "FROM web_notifications WHERE user_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (user_id, NOTIFICATION_EXPORT_LIMIT),
        ).fetchall()

        device_rows = self._conn.execute(
            # os_version 也是我们就这台设备收集的个人数据，导出里不能漏。
            "SELECT dt.id, dt.device_token, dt.env, dt.platform, dt.model, "
            "       dt.os_version, dt.bundle_id, dt.created_at, dt.last_seen "
            "FROM device_tokens dt "
            "JOIN app_tokens at ON dt.app_token_id = at.id "
            "WHERE at.user_id = ? AND dt.disabled_at IS NULL",
            (user_id,),
        ).fetchall()

        token_rows = self._conn.execute(
            "SELECT id, device_name, created_at, last_used_at, expires_at "
            "FROM app_tokens "
            "WHERE user_id = ? AND revoked = 0",
            (user_id,),
        ).fetchall()

        return {
            "notification_history": [dict(r) for r in notif_rows],
            "active_devices": [dict(r) for r in device_rows],
            "active_tokens": [dict(r) for r in token_rows],
        }
