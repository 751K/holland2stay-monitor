"""mstorage/_rounds.py — 抓取轮次遥测
=====================================

每轮每 source 一行，回答「历史上发生过什么」这类 ``meta.last_scrape_count``
（一个被反复覆盖的标量）回答不了的问题。

设计取舍
--------
**按 source 而不是按 task 记录。** task 粒度（每城市/每楼栋一行）能回答更细的
问题，但行数翻十几倍，而 monitor 的隔离边界本来就是 source——一个 source 塌了
是整体塌，单个城市的差异用 ``targets`` / ``complete`` 两个计数表达就够。

**写入必须失败无害。** 调用方一律用 ``record_round_stat`` 的返回值判断成功与否，
不指望异常——观测组件不该把被观测的抓取带崩。异常在这一层就吞掉。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

# 单条 error_msg 的存储上限。完整堆栈在 errors.log 里，这里只要够认出是哪类错误。
_ERR_MSG_MAX = 500

# 保留期与剪枝节流。4 source × 288 轮/天 × 30 天 ≈ 3.5 万行。
_RETENTION_DAYS = 30
_PRUNE_META_KEY = "round_stats_pruned_at"
_PRUNE_MIN_INTERVAL = 3600.0  # 每小时最多剪一次


class RoundStatsOps:
    """依赖 self._conn / self.get_meta / self.set_meta（来自 StorageBase）。"""

    # ── 写入 ────────────────────────────────────────────────────────

    def record_round_stat(
        self,
        *,
        round_at: str,
        source: str,
        listings: int = 0,
        targets: int = 0,
        complete: int = 0,
        duration_ms: int = 0,
        error_type: str = "",
        error_msg: str = "",
    ) -> bool:
        """记一行轮次遥测。任何异常都吞掉并返回 False。"""
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO round_stats "
                    "(round_at, source, listings, targets, complete, "
                    " duration_ms, error_type, error_msg) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (
                        round_at, source, int(listings), int(targets),
                        int(complete), int(duration_ms),
                        error_type or "", (error_msg or "")[:_ERR_MSG_MAX],
                    ),
                )
            return True
        except Exception:
            logger.debug("round_stats 写入失败（已忽略）", exc_info=True)
            return False

    # ── 剪枝 ────────────────────────────────────────────────────────

    def prune_round_stats(
        self,
        *,
        days: int = _RETENTION_DAYS,
        now: float | None = None,
        force: bool = False,
    ) -> int:
        """删掉保留期外的行，返回删除行数。

        自带节流：距上次剪枝不足一小时就直接返回 0。monitor 每轮都会调它，
        没有节流的话就是每 5 分钟一次全表 DELETE 扫描，纯浪费。
        """
        try:
            now_ts = time.time() if now is None else now
            if not force:
                try:
                    last = float(self.get_meta(_PRUNE_META_KEY, default="") or 0)
                except (TypeError, ValueError):
                    last = 0.0
                if now_ts - last < _PRUNE_MIN_INTERVAL:
                    return 0

            cutoff = (
                datetime.fromtimestamp(now_ts, tz=timezone.utc)
                - timedelta(days=days)
            ).isoformat()
            with self._conn:
                cur = self._conn.execute(
                    "DELETE FROM round_stats WHERE round_at < ?", (cutoff,)
                )
                deleted = cur.rowcount or 0
            self.set_meta(_PRUNE_META_KEY, str(now_ts))
            if deleted:
                logger.info("round_stats 剪枝：删除 %d 行（保留 %d 天）", deleted, days)
            return deleted
        except Exception:
            logger.debug("round_stats 剪枝失败（已忽略）", exc_info=True)
            return 0

    # ── 读取 ────────────────────────────────────────────────────────

    def recent_round_stats(
        self, *, source: str | None = None, limit: int = 200,
    ) -> list[dict[str, Any]]:
        """最近的遥测行，最新在前。"""
        sql = (
            "SELECT round_at, source, listings, targets, complete, "
            "       duration_ms, error_type, error_msg "
            "FROM round_stats "
        )
        params: list[Any] = []
        if source:
            sql += "WHERE source = ? "
            params.append(source)
        sql += "ORDER BY round_at DESC, id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        try:
            rows = self._conn.execute(sql, params).fetchall()
        except Exception:
            logger.debug("round_stats 查询失败（已忽略）", exc_info=True)
            return []
        return [dict(r) for r in rows]

    def round_stats_sources(self) -> list[str]:
        """遥测里出现过的 source，字母序。

        注意它答的是「抓过什么」而不是「配置了什么」——刚从 SOURCES 里摘掉的
        source 在保留期内仍会出现，这是想要的：排查的往往正是刚摘掉的那个。
        """
        try:
            rows = self._conn.execute(
                "SELECT DISTINCT source FROM round_stats ORDER BY source"
            ).fetchall()
        except Exception:
            return []
        return [r[0] for r in rows]

    def recent_rounds_grouped(self, *, limit: int = 30) -> list[dict[str, Any]]:
        """最近 N 轮，按 round_at 分组，每轮带上各 source 的明细。

        面板的「最近轮次」表格用。limit 数的是**轮数**，不是行数。
        """
        try:
            round_rows = self._conn.execute(
                "SELECT DISTINCT round_at FROM round_stats "
                "ORDER BY round_at DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        except Exception:
            logger.debug("round_stats 分组查询失败（已忽略）", exc_info=True)
            return []
        if not round_rows:
            return []

        stamps = [r[0] for r in round_rows]
        placeholders = ",".join("?" * len(stamps))
        rows = self._conn.execute(
            "SELECT round_at, source, listings, targets, complete, "
            "       duration_ms, error_type, error_msg "
            f"FROM round_stats WHERE round_at IN ({placeholders}) "
            "ORDER BY round_at DESC, source",
            stamps,
        ).fetchall()

        grouped: dict[str, dict[str, Any]] = {
            s: {"round_at": s, "sources": [], "listings": 0, "errors": 0}
            for s in stamps
        }
        for r in rows:
            g = grouped[r["round_at"]]
            g["sources"].append(dict(r))
            g["listings"] += r["listings"]
            if r["error_type"]:
                g["errors"] += 1
        return [grouped[s] for s in stamps]
