"""基础设施：连接管理、schema 迁移、meta 读写、生命周期。"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# 荷兰城市口语别称 → 正式名（提升 Photon 地理编码准确率）
_CITY_FORMAL: dict[str, str] = {
    "Den Bosch": "'s-Hertogenbosch",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class StorageBase:
    """数据库连接 + schema + meta + 生命周期（其他 mixin 依赖此类提供 self._conn / self._tz）。"""

    def __init__(self, db_path: Path, timezone_str: str = "UTC") -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._tz = timezone_str
        self._migrate()
        logger.debug("Storage 已连接: %s", db_path)

    # ── Schema ──────────────────────────────────────────────────────

    def _migrate(self) -> None:
        cur = self._conn.cursor()
        cur.executescript("""
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS listings (
                id              TEXT PRIMARY KEY,
                name            TEXT,
                status          TEXT,
                price_raw       TEXT,
                available_from  TEXT,
                features        TEXT,
                url             TEXT,
                city            TEXT,
                first_seen      TEXT,
                last_seen       TEXT,
                notified        INTEGER DEFAULT 0,
                last_status     TEXT
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
                key   TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS web_notifications (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                type        TEXT NOT NULL,
                title       TEXT NOT NULL,
                body        TEXT NOT NULL DEFAULT '',
                url         TEXT NOT NULL DEFAULT '',
                listing_id  TEXT NOT NULL DEFAULT '',
                read        INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS geocode_cache (
                address TEXT PRIMARY KEY,
                lat     REAL NOT NULL,
                lng     REAL NOT NULL
            );
        """)

    # ── Meta ────────────────────────────────────────────────────────

    def get_meta(self, key: str, default: str = "—") -> str:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key=?", (key,)
        ).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (key, value)
            )

    # ── 生命周期 ────────────────────────────────────────────────────

    def reset_all(self) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM listings")
            self._conn.execute("DELETE FROM status_changes")
            self._conn.execute("DELETE FROM meta")
            self._conn.execute("DELETE FROM web_notifications")
            self._conn.execute("DELETE FROM geocode_cache")
        logger.info("数据库已清空（全部 5 张表）")

    def close(self) -> None:
        self._conn.close()
