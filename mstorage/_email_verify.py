"""收件邮箱归属验证 token 的 SQLite CRUD。

设计要点
--------
- token 用 secrets.token_urlsafe(32)（≈43 字符）；URL-safe 可直接进邮件链接
- 有效期 24h；过期 token 不重用，按需新生成
- 每个 (user_id, email) 可有多条 token 记录（用户重发会产生多条），最新未过期者有效
- 实际验证逻辑（mark email_verified=1）由路由层处理：
  存储层只管 token 真伪 + 是否过期 + 关联的 user_id/email
- 不存 IP / UA / 任何 PII 之外的元数据，最小披露原则
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone


TOKEN_TTL_HOURS = 24


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_token() -> str:
    """URL-safe 32 字节随机 token，无填充。"""
    return secrets.token_urlsafe(32)


class EmailVerifyOps:
    """挂在 StorageBase 上：依赖 self._conn。"""

    def create_email_verification(self, user_id: str, email: str) -> str:
        """创建一条新的待验证记录，返回 token。"""
        token = _new_token()
        now = datetime.now(timezone.utc)
        expires = now + timedelta(hours=TOKEN_TTL_HOURS)
        with self._conn:
            self._conn.execute(
                "INSERT INTO email_verifications "
                "(token, user_id, email, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (token, user_id, email, now.isoformat(), expires.isoformat()),
            )
        return token

    def consume_email_verification(self, token: str) -> dict | None:
        """
        校验 token，命中且未过期未消费时返回 {user_id, email}，
        并把该 token 标记为已 verified（同一 token 二次点击仍返回，不抛错）。

        失效原因：token 不存在、已过期、或 user_id 不存在（外部清理）。
        返回 None 即视为无效，调用方应展示通用"链接已失效"页。
        """
        row = self._conn.execute(
            "SELECT user_id, email, expires_at, verified_at "
            "FROM email_verifications WHERE token = ?",
            (token,),
        ).fetchone()
        if row is None:
            return None
        user_id, email, expires_at, verified_at = (
            row["user_id"], row["email"], row["expires_at"], row["verified_at"],
        )
        # 过期 → 拒。即使已 verified，也不再让旧 token 参与状态写回；
        # 避免用户改走邮箱又改回后，历史链接重新解锁。
        try:
            exp_dt = datetime.fromisoformat(expires_at)
            if exp_dt < datetime.now(timezone.utc):
                return None
        except ValueError:
            return None
        # 已 verified 的 token 允许 24h 内重新点击（用户书签了链接）；
        # 仍返回 user_id/email，方便路由层把页面渲染成成功态。
        if verified_at:
            return {"user_id": user_id, "email": email}
        # 标记已验证（不删行，留审计痕迹）
        with self._conn:
            self._conn.execute(
                "UPDATE email_verifications SET verified_at = ? WHERE token = ?",
                (_now_iso(), token),
            )
        return {"user_id": user_id, "email": email}

    def prune_expired_verifications(self) -> int:
        """清理过期且已超期 30 天的 token，控制表大小。"""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM email_verifications WHERE expires_at < ?",
                (cutoff,),
            )
        return cur.rowcount or 0

    # ── Resend 配额（多 worker 共享） ────────────────────────────────
    # 设计：scope='global' key='' / scope='user' key=user_id；
    # 同一 (scope, key, day) UPSERT 累加。读路径只查 today 两行。

    def get_email_send_counts(
        self, day: str, user_id: str = "",
    ) -> tuple[int, int]:
        """返回 (global_count, per_user_count)。day 为 UTC YYYY-MM-DD。"""
        row_g = self._conn.execute(
            "SELECT count FROM email_send_counters "
            "WHERE scope='global' AND key='' AND day=?",
            (day,),
        ).fetchone()
        g = int(row_g[0]) if row_g else 0
        u = 0
        if user_id:
            row_u = self._conn.execute(
                "SELECT count FROM email_send_counters "
                "WHERE scope='user' AND key=? AND day=?",
                (user_id, day),
            ).fetchone()
            u = int(row_u[0]) if row_u else 0
        return g, u

    def record_email_send(self, day: str, user_id: str = "") -> None:
        """两个 scope 都 UPSERT +1。同一事务，避免半成功。"""
        with self._conn:
            self._conn.execute(
                "INSERT INTO email_send_counters (scope, key, day, count) "
                "VALUES ('global', '', ?, 1) "
                "ON CONFLICT(scope, key, day) DO UPDATE SET count = count + 1",
                (day,),
            )
            if user_id:
                self._conn.execute(
                    "INSERT INTO email_send_counters (scope, key, day, count) "
                    "VALUES ('user', ?, ?, 1) "
                    "ON CONFLICT(scope, key, day) DO UPDATE SET count = count + 1",
                    (user_id, day),
                )

    def prune_old_email_send_counters(self, keep_days: int = 30) -> int:
        """清理超期计数行，控制表大小。建议每天调一次。"""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=keep_days)
        ).strftime("%Y-%m-%d")
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM email_send_counters WHERE day < ?",
                (cutoff,),
            )
        return cur.rowcount or 0
