"""
APNs 设备 token 持久化
=======================

模型
----
``device_tokens`` 表记录 iOS App 注册的 APNs device token。

每个 device row 通过 ``app_token_id`` 外键关联到 ``app_tokens`` 表：
- 会话被撤销或过期 → 该会话的设备不再可推送（``get_active_devices_for_*``
  会 JOIN ``app_tokens`` 按 ``revoked`` 与 ``expires_at`` 过滤掉）
- 用户重新登录 → 通常会拿到新 app_token + 重新注册设备 → UNIQUE
  ``(app_token_id, device_token)`` 保证幂等

**一台设备只归属最后一次注册的那个会话。** 表结构按会话分行，但 APNs 的
device token 是「一个 App 安装 = 一个 token」，所以同一 token 出现在新会话下
只意味着同一台设备换了个登录。``register_device`` 会把它在其它会话下的旧行
停掉（``disabled_reason='SupersededBySession'``），否则换账号而客户端没能调成
``/auth/logout`` 时，一台设备会同时收到新旧两个账号的通知——2026-08-07 生产
实测有两台中招。见 ``_retire_stale_rows_for``。

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
        language: str = "en",
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
                          language = ?,
                          last_seen = ?,
                          disabled_at = NULL, disabled_reason = NULL
                       WHERE id = ?""",
                    (env, platform, model, bundle_id, language, now, row["id"]),
                )
                # 复活这一行的同时也要停掉别的：用户可能 A→B→A 地切回来，
                # 此时 B 的行还活着。
                self._retire_stale_rows_for(device_token, keep_id=int(row["id"]), now=now)
                return int(row["id"])
            cur = self._conn.execute(
                """INSERT INTO device_tokens
                       (app_token_id, device_token, env, platform,
                        model, bundle_id, language, created_at, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (app_token_id, device_token, env, platform,
                 model, bundle_id, language, now, now),
            )
            new_id = int(cur.lastrowid)  # type: ignore[arg-type]
            self._retire_stale_rows_for(device_token, keep_id=new_id, now=now)
            return new_id

    def _retire_stale_rows_for(self, device_token: str, *, keep_id: int, now: str) -> int:
        """把同一个 device_token 挂在**其它会话**下的行停掉。返回停掉的行数。

        APNs 的 device token 是「一个 App 安装 = 一个 token」。同一个 token 出现
        在新会话下，只可能是同一台设备上的同一个 App 换了个登录——旧会话是上一
        次登录的残留，不是另一台设备。

        不这么做的后果（2026-08-07 生产实测，两台设备中招）：

            9e660f7c…  user=c622bf26  登录 06-07  ← 旧账号，仍在推
                       user=ffdfe243  登录 08-03  ← 新账号
            500e00ed…  两个 admin 会话都活着      ← 同一条推送发两遍

        ``device_tokens`` 绑的是 ``app_token_id``（会话）而不是设备，而
        ``get_active_devices_for_user`` 只按 ``revoked`` 过滤。换账号时客户端
        如果没调 ``/auth/logout``，或者调了但离线失败，旧会话就一直是
        ``revoked=0``——于是**一台设备同时收两个账号的通知**。

        只停设备行，不撤销旧 app_token：那是登录态策略，不该由一次推送注册
        顺手决定。

        用 ``disabled_at`` 而不是删行：保留审计痕迹，且用户若切回旧账号，
        ``register_device`` 会把对应行的 ``disabled_at`` 清空复活。
        """
        cur = self._conn.execute(
            """UPDATE device_tokens
                  SET disabled_at = ?, disabled_reason = 'SupersededBySession'
                WHERE device_token = ? AND id != ? AND disabled_at IS NULL""",
            (now, device_token, keep_id),
        )
        n = cur.rowcount or 0
        if n:
            logger.info(
                "设备 %s… 已在新会话下注册，停掉它在其它 %d 个会话下的旧行",
                device_token[:12], n,
            )
        return n

    # ── 查询 ────────────────────────────────────────────────────────

    def list_all_devices(self) -> list[dict]:
        """列出所有推送设备（含 disabled），JOIN app_tokens 拿到 user/role。"""
        rows = self._conn.execute(
            """SELECT d.id, d.device_token, d.platform, d.env, d.model,
                      d.bundle_id, d.language, d.created_at, d.last_seen,
                      d.disabled_at, d.disabled_reason,
                      t.user_id, t.role, t.device_name
               FROM device_tokens d
               JOIN app_tokens t ON d.app_token_id = t.id
               ORDER BY d.id DESC"""
        ).fetchall()
        return [dict(r) for r in rows]

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
        - app_tokens.user_id = ? AND revoked = 0 AND 未过期
        - device_tokens.disabled_at IS NULL

        过期判据不能省。``revoked`` 是「被显式撤销」，``expires_at`` 是「自己到
        期」，两者互不蕴含：一个到期的会话在 API 鉴权那边已经用不了了，却仍然
        满足 ``revoked = 0``——不看过期就等于给已经登出的设备继续推送。

        2026-08-07 查的时候受影响设备是 0 台，但那只是因为 90 天 TTL 一个都还
        没到期（最早的一个 2026-08-19 到），不是因为逻辑对。
        """
        rows = self._conn.execute(
            """SELECT d.id, d.device_token, d.env, d.platform,
                      d.model, d.bundle_id, d.app_token_id, d.language
               FROM device_tokens d
               JOIN app_tokens t ON d.app_token_id = t.id
               WHERE t.user_id = ?
                 AND t.revoked = 0
                 AND (t.expires_at IS NULL OR t.expires_at >= ?)
                 AND d.disabled_at IS NULL
               ORDER BY d.id DESC""",
            (user_id, _utc_now_iso()),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_active_devices_for_admin(self) -> list[dict]:
        """
        所有 admin 角色当前可推送的设备。

        条件：
        - app_tokens.role = 'admin' AND user_id IS NULL AND revoked = 0 AND 未过期
        - device_tokens.disabled_at IS NULL

        过期判据同 ``get_active_devices_for_user``。
        """
        rows = self._conn.execute(
            """SELECT d.id, d.device_token, d.env, d.platform,
                      d.model, d.bundle_id, d.app_token_id, d.language
               FROM device_tokens d
               JOIN app_tokens t ON d.app_token_id = t.id
               WHERE t.role = 'admin'
                 AND t.user_id IS NULL
                 AND t.revoked = 0
                 AND (t.expires_at IS NULL OR t.expires_at >= ?)
                 AND d.disabled_at IS NULL
               ORDER BY d.id DESC""",
            (_utc_now_iso(),),
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
