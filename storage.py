from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from models import Listing

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Storage:
    """
    SQLite 持久化层。负责：
    1. 记录历史上见过的所有房源快照
    2. 跟踪哪些房源已经发过通知
    3. 检测新房源 & 状态变更
    """

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._migrate()
        logger.debug("Storage 已连接: %s", db_path)

    # ------------------------------------------------------------------ #
    # Schema
    # ------------------------------------------------------------------ #

    def _migrate(self) -> None:
        cur = self._conn.cursor()
        cur.executescript("""
            CREATE TABLE IF NOT EXISTS listings (
                id              TEXT PRIMARY KEY,
                name            TEXT,
                status          TEXT,
                price_raw       TEXT,
                available_from  TEXT,
                features        TEXT,   -- JSON array
                url             TEXT,
                city            TEXT,
                first_seen      TEXT,
                last_seen       TEXT,
                notified        INTEGER DEFAULT 0,  -- 0=未通知, 1=已通知
                last_status     TEXT    -- 用于检测状态变化
            );

            CREATE TABLE IF NOT EXISTS status_changes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id  TEXT,
                old_status  TEXT,
                new_status  TEXT,
                changed_at  TEXT,
                notified    INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS meta (
                key     TEXT PRIMARY KEY,
                value   TEXT
            );
        """)
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # 核心：对比本次抓取结果与已知状态，返回需要通知的事件
    # ------------------------------------------------------------------ #

    def diff(
        self, fresh: list[Listing]
    ) -> tuple[list[Listing], list[tuple[Listing, str, str]]]:
        """
        将本次抓取结果与数据库对比，返回：
          new_listings    : 全新房源（从未见过）
          status_changes  : [(listing, old_status, new_status), ...]

        同时更新数据库快照。
        """
        now = _now_iso()
        new_listings: list[Listing] = []
        status_changes: list[tuple[Listing, str, str]] = []

        cur = self._conn.cursor()

        for listing in fresh:
            row = cur.execute(
                "SELECT status, notified FROM listings WHERE id = ?",
                (listing.id,),
            ).fetchone()

            if row is None:
                # 全新房源
                cur.execute(
                    """INSERT INTO listings
                       (id, name, status, price_raw, available_from,
                        features, url, city, first_seen, last_seen, notified, last_status)
                       VALUES (?,?,?,?,?,?,?,?,?,?,0,?)""",
                    (
                        listing.id, listing.name, listing.status,
                        listing.price_raw, listing.available_from,
                        json.dumps(listing.features, ensure_ascii=False),
                        listing.url, listing.city, now, now, listing.status,
                    ),
                )
                new_listings.append(listing)
            else:
                # 已知房源：更新快照，检测状态变更
                old_status = row["status"]
                cur.execute(
                    """UPDATE listings
                       SET name=?, status=?, price_raw=?, available_from=?,
                           features=?, last_seen=?, last_status=?
                       WHERE id=?""",
                    (
                        listing.name, listing.status, listing.price_raw,
                        listing.available_from,
                        json.dumps(listing.features, ensure_ascii=False),
                        now, listing.status, listing.id,
                    ),
                )
                if old_status != listing.status:
                    cur.execute(
                        """INSERT INTO status_changes
                           (listing_id, old_status, new_status, changed_at)
                           VALUES (?,?,?,?)""",
                        (listing.id, old_status, listing.status, now),
                    )
                    status_changes.append((listing, old_status, listing.status))

        self._conn.commit()
        return new_listings, status_changes

    # ------------------------------------------------------------------ #
    # 通知回执
    # ------------------------------------------------------------------ #

    def mark_notified(self, listing_id: str) -> None:
        self._conn.execute(
            "UPDATE listings SET notified=1 WHERE id=?", (listing_id,)
        )
        self._conn.commit()

    def mark_status_change_notified(self, listing_id: str) -> None:
        self._conn.execute(
            """UPDATE status_changes SET notified=1
               WHERE listing_id=? AND notified=0""",
            (listing_id,),
        )
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # 查询工具（供 monitor.py 日志 / 统计使用）
    # ------------------------------------------------------------------ #

    def count_all(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM listings").fetchone()
        return row[0] if row else 0

    def get_listing(self, listing_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM listings WHERE id=?", (listing_id,)
        ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------ #
    # 查询（Web 面板用）
    # ------------------------------------------------------------------ #

    def get_all_listings(
        self,
        status: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 500,
    ) -> list[dict]:
        q = "SELECT * FROM listings WHERE 1=1"
        params: list = []
        if status:
            q += " AND status = ?"
            params.append(status)
        if search:
            q += " AND (name LIKE ? OR city LIKE ?)"
            params.extend([f"%{search}%", f"%{search}%"])
        q += " ORDER BY first_seen DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._conn.execute(q, params).fetchall()]

    def get_recent_changes(self, hours: int = 48) -> list[dict]:
        from datetime import timedelta
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = self._conn.execute(
            """SELECT sc.*, l.name, l.url, l.price_raw
               FROM status_changes sc
               JOIN listings l ON l.id = sc.listing_id
               WHERE sc.changed_at > ?
               ORDER BY sc.changed_at DESC""",
            (since,),
        ).fetchall()
        return [dict(r) for r in rows]

    def count_new_since(self, hours: int = 24) -> int:
        from datetime import timedelta
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        row = self._conn.execute(
            "SELECT COUNT(*) FROM listings WHERE first_seen > ?", (since,)
        ).fetchone()
        return row[0] if row else 0

    def count_changes_since(self, hours: int = 24) -> int:
        from datetime import timedelta
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        row = self._conn.execute(
            "SELECT COUNT(*) FROM status_changes WHERE changed_at > ?", (since,)
        ).fetchone()
        return row[0] if row else 0

    def get_distinct_statuses(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT status FROM listings ORDER BY status"
        ).fetchall()
        return [r[0] for r in rows]

    # ------------------------------------------------------------------ #
    # Meta（键值存储，供面板显示最近抓取时间等）
    # ------------------------------------------------------------------ #

    def get_meta(self, key: str, default: str = "—") -> str:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key=?", (key,)
        ).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (key, value)
        )
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # 图表数据（Chart.js）
    # ------------------------------------------------------------------ #

    def chart_daily_new(self, days: int = 30) -> list[dict]:
        """近 N 天每天新增房源数，补零保证天数连续。"""
        rows = self._conn.execute(
            """
            SELECT date(first_seen) AS day, COUNT(*) AS cnt
            FROM listings
            WHERE first_seen >= date('now', ?, 'localtime')
            GROUP BY day
            ORDER BY day
            """,
            (f"-{days} days",),
        ).fetchall()
        return [{"date": r["day"], "count": r["cnt"]} for r in rows]

    def chart_daily_changes(self, days: int = 30) -> list[dict]:
        """近 N 天每天状态变更数。"""
        rows = self._conn.execute(
            """
            SELECT date(changed_at) AS day, COUNT(*) AS cnt
            FROM status_changes
            WHERE changed_at >= date('now', ?, 'localtime')
            GROUP BY day
            ORDER BY day
            """,
            (f"-{days} days",),
        ).fetchall()
        return [{"date": r["day"], "count": r["cnt"]} for r in rows]

    def chart_city_dist(self) -> list[dict]:
        """按城市统计当前房源数量。"""
        rows = self._conn.execute(
            """
            SELECT COALESCE(NULLIF(city,''), '未知') AS city, COUNT(*) AS cnt
            FROM listings
            GROUP BY city
            ORDER BY cnt DESC
            """,
        ).fetchall()
        return [{"city": r["city"], "count": r["cnt"]} for r in rows]

    def chart_status_dist(self) -> list[dict]:
        """按状态统计当前房源数量。"""
        rows = self._conn.execute(
            """
            SELECT COALESCE(NULLIF(status,''), '未知') AS status, COUNT(*) AS cnt
            FROM listings
            GROUP BY status
            ORDER BY cnt DESC
            """,
        ).fetchall()
        return [{"status": r["status"], "count": r["cnt"]} for r in rows]

    def chart_price_dist(self) -> list[dict]:
        """按租金区间统计房源数量（Python 端解析 price_raw）。"""
        import re as _re
        rows = self._conn.execute(
            "SELECT price_raw FROM listings WHERE price_raw IS NOT NULL AND price_raw != ''"
        ).fetchall()

        buckets: dict[str, int] = {
            "<€600":      0,
            "€600-700":   0,
            "€700-800":   0,
            "€800-900":   0,
            "€900-1000":  0,
            ">€1000":     0,
        }
        for (raw,) in rows:
            m = _re.search(r"[\d]+[,\d]*\.?\d*", (raw or "").replace(",", ""))
            if not m:
                continue
            try:
                price = float(m.group())
            except ValueError:
                continue
            if price < 600:
                buckets["<€600"] += 1
            elif price < 700:
                buckets["€600-700"] += 1
            elif price < 800:
                buckets["€700-800"] += 1
            elif price < 900:
                buckets["€800-900"] += 1
            elif price < 1000:
                buckets["€900-1000"] += 1
            else:
                buckets[">€1000"] += 1

        return [{"range": k, "count": v} for k, v in buckets.items()]

    def close(self) -> None:
        self._conn.close()
