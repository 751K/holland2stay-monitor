"""
Bearer Token 持久化（iOS / 第三方客户端用）
==============================================

模型
----
``app_tokens`` 表存储 iOS / 第三方客户端登录后签发的 Bearer 令牌。
明文 token 只在 ``create_app_token()`` 返回一次，之后只能通过
``sha256(token)`` 反查，库中**不**保留可逆形式。

字段
----
- id           : 自增主键
- token_hash   : sha256(token) 十六进制，UNIQUE
- role         : "admin" / "user"
- user_id      : role=user 时指向 UserConfig.id；admin 为 NULL
- device_name  : 设备显示名（"iPhone 15 Pro"），便于撤销时辨认
- created_at   : ISO8601 UTC
- last_used_at : ISO8601 UTC；异步批量刷新（见 touch_app_tokens）
- expires_at   : ISO8601 UTC，NULL = 永不过期
- revoked      : 0/1，1 表示已撤销（保留行便于审计）

设计要点
--------
1. 验证只通过 ``find_app_token(token_hash)``，调用方对 ``token_hash``
   先 SHA-256（不要传明文）。
2. ``last_used_at`` 不在每次验证时写库——会让每个 API 请求多一次 SQLite
   写入。改用 ``touch_app_tokens(ids)`` 批量更新，由 api_auth 层在内存
   累计后定期 flush。
3. 撤销不删除行（``revoked=1``），保留历史便于审计；
   ``cleanup_expired_tokens()`` 提供物理清理（按需调用）。
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hash_token(plaintext: str) -> str:
    """SHA-256(token) 十六进制——查询时唯一索引。"""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def generate_token() -> str:
    """生成 256-bit 随机 token（base64url，无 padding，长度 43）。"""
    return secrets.token_urlsafe(32)


class TokenOps:
    """依赖 self._conn（由 StorageBase 提供）。"""

    # ── 签发 ────────────────────────────────────────────────────────

    def create_app_token(
        self,
        *,
        role: str,
        user_id: Optional[str],
        device_name: str = "",
        ttl_days: Optional[int] = 90,
    ) -> tuple[int, str]:
        """
        签发一枚新 token。

        Parameters
        ----------
        role        : "admin" 或 "user"
        user_id     : role="user" 时 UserConfig.id；role="admin" 时传 None
        device_name : 设备名，便于撤销时辨认（"iPhone 15 Pro"）
        ttl_days    : 有效期天数；传 None 表示永不过期

        Returns
        -------
        (token_id, plaintext_token)
            plaintext_token 是**返回给客户端的明文**，库中只存 SHA-256。
            调用方必须在响应里返回给客户端，并永远不再持久化明文。
        """
        if role not in ("admin", "user"):
            raise ValueError(f"invalid role: {role!r}")
        if role == "user" and not user_id:
            raise ValueError("role=user 必须提供 user_id")
        if role == "admin" and user_id:
            raise ValueError("role=admin 不应提供 user_id")

        plaintext = generate_token()
        token_hash = hash_token(plaintext)
        now = _utc_now_iso()
        expires_at: Optional[str] = None
        if ttl_days is not None:
            expires_at = (
                datetime.now(timezone.utc) + timedelta(days=ttl_days)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")

        with self._conn:
            cur = self._conn.execute(
                """INSERT INTO app_tokens
                       (token_hash, role, user_id, device_name,
                        created_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (token_hash, role, user_id, device_name, now, expires_at),
            )
            token_id = cur.lastrowid
        return int(token_id), plaintext  # type: ignore[arg-type]

    # ── 查询 ────────────────────────────────────────────────────────

    def find_app_token(self, token_hash: str) -> Optional[dict]:
        """
        根据 SHA-256 哈希查 token；返回 dict 或 None。

        **不**自动过滤 revoked / expired，由 api_auth 层判断
        （后者也要把这些原因区分上报给客户端）。
        """
        row = self._conn.execute(
            "SELECT * FROM app_tokens WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        return dict(row) if row else None

    def list_app_tokens(
        self,
        *,
        user_id: Optional[str] = None,
        include_revoked: bool = False,
    ) -> list[dict]:
        """
        列出 token 用于管理页 / 审计。

        - user_id=None 且 include_revoked=False → 所有未撤销的 token（admin 用）
        - user_id="abcd1234"                    → 该用户的所有 token（自助管理）
        - include_revoked=True                  → 包含已撤销
        """
        q = "SELECT id, role, user_id, device_name, created_at, " \
            "last_used_at, expires_at, revoked FROM app_tokens"
        clauses: list[str] = []
        params: list = []
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)
        if not include_revoked:
            clauses.append("revoked = 0")
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY created_at DESC"
        rows = self._conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    # ── 状态变更 ────────────────────────────────────────────────────

    def revoke_app_token(self, token_id: int) -> bool:
        """撤销单条 token；返回是否真的修改了一行。"""
        with self._conn:
            cur = self._conn.execute(
                "UPDATE app_tokens SET revoked = 1 WHERE id = ? AND revoked = 0",
                (token_id,),
            )
            return cur.rowcount > 0

    def revoke_user_tokens(self, user_id: str) -> int:
        """
        撤销某 user 名下的所有 token；返回撤销数量。

        典型场景：用户在 Web 后台改密码 / 删除账号时连带失效所有 App 会话。
        """
        with self._conn:
            cur = self._conn.execute(
                "UPDATE app_tokens SET revoked = 1 "
                "WHERE user_id = ? AND revoked = 0",
                (user_id,),
            )
            return cur.rowcount

    def touch_app_tokens(self, token_ids: list[int]) -> None:
        """
        批量刷新 last_used_at（异步队列调用，每请求不直接走这里）。

        空列表是 no-op，便于调用方无脑调用。
        """
        if not token_ids:
            return
        now = _utc_now_iso()
        placeholders = ",".join("?" * len(token_ids))
        with self._conn:
            self._conn.execute(
                f"UPDATE app_tokens SET last_used_at = ? "
                f"WHERE id IN ({placeholders})",
                [now, *token_ids],
            )

    # ── 维护 ────────────────────────────────────────────────────────

    def cleanup_expired_tokens(self, *, keep_revoked_days: int = 30) -> int:
        """
        物理删除：1) 已撤销且 > N 天的；2) 已过期且 > N 天的。

        返回删除行数。按需在定时任务中调用，正常运行不需要。
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=keep_revoked_days)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._conn:
            cur = self._conn.execute(
                """DELETE FROM app_tokens
                   WHERE (revoked = 1 AND created_at < ?)
                      OR (expires_at IS NOT NULL AND expires_at < ?)""",
                (cutoff, cutoff),
            )
            return cur.rowcount
