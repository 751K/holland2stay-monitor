"""房源 CRUD：diff、标记已通知、面板列表、filter 辅助查询。"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from models import Listing

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ListingOps:
    """依赖 self._conn / self._tz（由 StorageBase.__init__ 提供）。"""

    # ── diff（核心）──────────────────────────────────────────────────

    def diff(
        self, fresh: list[Listing]
    ) -> tuple[list[Listing], list[tuple[Listing, str, str]]]:
        now = _now_iso()
        new_listings: list[Listing] = []
        status_changes: list[tuple[Listing, str, str]] = []

        cur = self._conn.cursor()
        with self._conn:
            ids = [l.id for l in fresh]
            existing: dict[str, str] = {}
            if ids:
                placeholders = ",".join("?" * len(ids))
                rows = cur.execute(
                    f"SELECT id, status FROM listings WHERE id IN ({placeholders})",
                    ids,
                ).fetchall()
                existing = {r["id"]: r["status"] for r in rows}

            for listing in fresh:
                old_status = existing.get(listing.id)

                if old_status is None:
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

        return new_listings, status_changes

    # ── 通知回执 ────────────────────────────────────────────────────

    def mark_notified(self, listing_id: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE listings SET notified=1 WHERE id=?", (listing_id,)
            )

    def mark_notified_batch(self, listing_ids: list[str]) -> None:
        if not listing_ids:
            return
        with self._conn:
            placeholders = ",".join("?" for _ in listing_ids)
            self._conn.execute(
                f"UPDATE listings SET notified=1 WHERE id IN ({placeholders})",
                listing_ids,
            )

    def mark_status_change_notified(self, listing_id: str) -> None:
        with self._conn:
            self._conn.execute(
                """UPDATE status_changes SET notified=1
                   WHERE listing_id=? AND notified=0""",
                (listing_id,),
            )

    def mark_status_change_notified_batch(self, listing_ids: list[str]) -> None:
        if not listing_ids:
            return
        with self._conn:
            for lid in listing_ids:
                self._conn.execute(
                    """UPDATE status_changes SET notified=1
                       WHERE listing_id=? AND notified=0""",
                    (lid,),
                )

    # ── 基础查询 ────────────────────────────────────────────────────

    def get_distinct_cities(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT city FROM listings WHERE city != '' ORDER BY city"
        ).fetchall()
        return [r[0] for r in rows]

    def count_all(self, city: Optional[str] = None) -> int:
        if city:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM listings WHERE city = ?", (city,)
            ).fetchone()
        else:
            row = self._conn.execute("SELECT COUNT(*) FROM listings").fetchone()
        return row[0] if row else 0

    def get_listing(self, listing_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM listings WHERE id=?", (listing_id,)
        ).fetchone()
        return dict(row) if row else None

    # ── 面板查询 ────────────────────────────────────────────────────

    def get_all_listings(
        self,
        status: Optional[str] = None,
        search: Optional[str] = None,
        city: Optional[str] = None,
        limit: int = 500,
    ) -> list[dict]:
        q = "SELECT * FROM listings WHERE 1=1"
        params: list = []
        if status:
            q += " AND status = ?"
            params.append(status)
        if search:
            q += " AND name LIKE ?"
            params.append(f"%{search}%")
        if city:
            q += " AND city = ?"
            params.append(city)
        q += " ORDER BY first_seen DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._conn.execute(q, params).fetchall()]

    def get_recent_changes(
        self, hours: int = 48, city: Optional[str] = None
    ) -> list[dict]:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        if city:
            rows = self._conn.execute(
                """SELECT sc.*, l.name, l.url, l.price_raw
                   FROM status_changes sc
                   JOIN listings l ON l.id = sc.listing_id
                   WHERE sc.changed_at > ? AND l.city = ?
                   ORDER BY sc.changed_at DESC""",
                (since, city),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT sc.*, l.name, l.url, l.price_raw
                   FROM status_changes sc
                   JOIN listings l ON l.id = sc.listing_id
                   WHERE sc.changed_at > ?
                   ORDER BY sc.changed_at DESC""",
                (since,),
            ).fetchall()
        return [dict(r) for r in rows]

    def count_new_since(
        self, hours: int = 24, city: Optional[str] = None
    ) -> int:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        if city:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM listings WHERE first_seen > ? AND city = ?",
                (since, city),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM listings WHERE first_seen > ?", (since,)
            ).fetchone()
        return row[0] if row else 0

    def count_changes_since(
        self, hours: int = 24, city: Optional[str] = None
    ) -> int:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        if city:
            row = self._conn.execute(
                """SELECT COUNT(*) FROM status_changes sc
                   JOIN listings l ON l.id = sc.listing_id
                   WHERE sc.changed_at > ? AND l.city = ?""",
                (since, city),
            ).fetchone()
        else:
            row = self._conn.execute(
                """SELECT COUNT(*) FROM status_changes sc
                   JOIN listings l ON l.id = sc.listing_id
                   WHERE sc.changed_at > ?""",
                (since,),
            ).fetchone()
        return row[0] if row else 0

    # ── 面板筛选辅助 ────────────────────────────────────────────────

    def get_distinct_statuses(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT status FROM listings ORDER BY status"
        ).fetchall()
        return [r[0] for r in rows]

    def get_feature_values(
        self,
        category: str,
        cities: Optional[list[str]] = None,
    ) -> list[str]:
        pattern = f"{category}:%"
        if cities:
            placeholders = ",".join("?" * len(cities))
            rows = self._conn.execute(
                f"""SELECT DISTINCT ltrim(substr(value, instr(value, ':') + 1)) AS val
                    FROM listings, json_each(features)
                    WHERE value LIKE ? AND city IN ({placeholders})
                    ORDER BY val""",
                [pattern, *cities],
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT DISTINCT ltrim(substr(value, instr(value, ':') + 1)) AS val
                   FROM listings, json_each(features)
                   WHERE value LIKE ?
                   ORDER BY val""",
                (pattern,),
            ).fetchall()
        return [r[0] for r in rows if r[0]]
