"""
APNs 设备 token 持久化
=======================

模型
----
``device_tokens`` 表记录 iOS App 注册的 APNs device token。

每个 device row 通过 ``app_token_id`` 外键关联到 ``app_tokens`` 表：
- 会话被撤销 → 该会话的设备自然不再可推送（``get_active_devices_for_user``
  会 JOIN ``app_tokens.revoked = 0`` 过滤掉）
- 用户重新登录 → 通常会拿到新 app_token + 重新注册设备 → UNIQUE
  ``(app_token_id, device_token)`` 保证幂等

字段
----
- env             : 'production' | 'sandbox'。TestFlight = production
                    （这是常见坑：sandbox 仅 Xcode 调试构建直连真机才用）
- model           : "iPhone15,2" 等，展示用
- bundle_id       : 防 Bundle ID 配错；客户端注册时上报
- disabled_at     : APNs 返回 410/400 时填入，停止后续发送
- disabled_reason : "Unregistered" / "BadDeviceToken" 等，便于排查
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class DeviceOps:
    """依赖 self._conn。"""

    # ── 注册 ────────────────────────────────────────────────────────

    def register_device(
        self,
        *,
        app_token_id: int,
        device_token: str,
        env: str = "production",
        platform: str = "ios",
        model: str = "",
        bundle_id: str = "",
    ) -> int:
        """
        注册或刷新一台设备的 APNs token。

        - 第一次：插入新行
        - 同 (app_token_id, device_token) 再次注册：刷新 last_seen + env/model
        - 已 disabled 的：清空 disabled_at（用户重装 App 时复活）

        Returns 设备行 id。
        """
        if env not in ("production", "sandbox"):
            raise ValueError(f"invalid env: {env!r}")
        if not device_token or len(device_token) < 32:
            raise ValueError("device_token 长度不合理")

        now = _utc_now_iso()
        with self._conn:
            cur = self._conn.execute(
                "SELECT id FROM device_tokens "
                "WHERE app_token_id = ? AND device_token = ?",
                (app_token_id, device_token),
            )
            row = cur.fetchone()
            if row:
                self._conn.execute(
                    """UPDATE device_tokens SET
                          env = ?, platform = ?, model = ?, bundle_id = ?,
                          last_seen = ?,
                          disabled_at = NULL, disabled_reason = NULL
                       WHERE id = ?""",
                    (env, platform, model, bundle_id, now, row["id"]),
                )
                return int(row["id"])
            cur = self._conn.execute(
                """INSERT INTO device_tokens
                       (app_token_id, device_token, env, platform,
                        model, bundle_id, created_at, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (app_token_id, device_token, env, platform,
                 model, bundle_id, now, now),
            )
            return int(cur.lastrowid)  # type: ignore[arg-type]

    # ── 查询 ────────────────────────────────────────────────────────

    def list_devices_for_token(self, app_token_id: int) -> list[dict]:
        """列出某会话名下的所有设备（含 disabled）。"""
        rows = self._conn.execute(
            "SELECT * FROM device_tokens WHERE app_token_id = ? "
            "ORDER BY id DESC",
            (app_token_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_active_devices_for_user(self, user_id: str) -> list[dict]:
        """
        某 user 当前所有可推送的设备。

        条件：
        - app_tokens.user_id = ? AND revoked = 0
        - device_tokens.disabled_at IS NULL
        """
        rows = self._conn.execute(
            """SELECT d.id, d.device_token, d.env, d.platform,
                      d.model, d.bundle_id, d.app_token_id
               FROM device_tokens d
               JOIN app_tokens t ON d.app_token_id = t.id
               WHERE t.user_id = ?
                 AND t.revoked = 0
                 AND d.disabled_at IS NULL
               ORDER BY d.id DESC""",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_device(self, device_id: int) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM device_tokens WHERE id = ?", (device_id,),
        ).fetchone()
        return dict(row) if row else None

    # ── 状态变更 ────────────────────────────────────────────────────

    def disable_device(
        self,
        device_id: int,
        reason: str = "",
    ) -> bool:
        """
        APNs 返回 410/400 时调，软停推送（保留行便于审计）。
        返回是否真的修改了一行（已 disabled 时为 False）。
        """
        with self._conn:
            cur = self._conn.execute(
                """UPDATE device_tokens
                   SET disabled_at = ?, disabled_reason = ?
                   WHERE id = ? AND disabled_at IS NULL""",
                (_utc_now_iso(), reason[:120], device_id),
            )
            return cur.rowcount > 0

    def disable_device_by_token(
        self,
        device_token: str,
        reason: str = "",
    ) -> int:
        """
        按 APNs device_token 失效——可能命中多个 app_token_id（同设备多次登录）。
        返回失效行数。
        """
        with self._conn:
            cur = self._conn.execute(
                """UPDATE device_tokens
                   SET disabled_at = ?, disabled_reason = ?
                   WHERE device_token = ? AND disabled_at IS NULL""",
                (_utc_now_iso(), reason[:120], device_token),
            )
            return cur.rowcount

    def delete_device(self, device_id: int) -> bool:
        """用户在 App 设置里主动登出某设备时硬删；返回是否删了。"""
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM device_tokens WHERE id = ?", (device_id,),
            )
            return cur.rowcount > 0
