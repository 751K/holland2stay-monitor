"""房源 CRUD：diff、标记已通知、面板列表、filter 辅助查询。"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from models import Listing

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _booking_hold_minutes() -> int:
    raw = os.environ.get("BOOKING_STATUS_HOLD_MINUTES", "120")
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 120


class ListingOps:
    """依赖 self._conn / self._tz（由 StorageBase.__init__ 提供）。"""

    # ── diff（核心）──────────────────────────────────────────────────

    def diff(
        self, fresh: list[Listing]
    ) -> tuple[list[Listing], list[tuple[Listing, str, str]]]:
        now = _now_iso()
        now_dt = datetime.now(timezone.utc)
        new_listings: list[Listing] = []
        status_changes: list[tuple[Listing, str, str]] = []

        cur = self._conn.cursor()
        with self._conn:
            ids = [l.id for l in fresh]
            existing: dict[str, dict] = {}
            if ids:
                placeholders = ",".join("?" * len(ids))
                rows = cur.execute(
                    f"""SELECT id, status, status_is_inferred, status_hold_until
                        FROM listings WHERE id IN ({placeholders})""",
                    ids,
                ).fetchall()
                existing = {r["id"]: dict(r) for r in rows}

            for listing in fresh:
                old_row = existing.get(listing.id)
                old_status = old_row["status"] if old_row is not None else None

                if old_status is None:
                    # P0: 写入 source 字段。老的 INSERT 不传 source 时
                    # 走 schema 默认值 'holland2stay'，但 Listing.source 已
                    # 在 scrapers 层强制赋值，这里直接传，更显式。
                    cur.execute(
                        """INSERT INTO listings
                           (id, name, status, price_raw, available_from,
                            features, url, city, first_seen, last_seen, notified, last_status,
                            source)
                           VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?)""",
                        (
                            listing.id, listing.name, listing.status,
                            listing.price_raw, listing.available_from,
                            json.dumps(listing.features, ensure_ascii=False),
                            listing.url, listing.city, now, now, listing.status,
                            listing.source,
                        ),
                    )
                    new_listings.append(listing)
                else:
                    if self._should_keep_booking_hold(old_row, listing.status, now_dt):
                        cur.execute(
                            """UPDATE listings
                               SET name=?, price_raw=?, available_from=?,
                                   features=?, last_seen=?, source=?
                               WHERE id=?""",
                            (
                                listing.name, listing.price_raw, listing.available_from,
                                json.dumps(listing.features, ensure_ascii=False),
                                now, listing.source, listing.id,
                            ),
                        )
                        continue

                    # 来自 API 的真实数据：复位 status_is_inferred=0，
                    # 撤销之前 mark_stale_listings 可能打过的"推测"标记。
                    # source 在 UPDATE 时也带上——理论上 listing 的 source 永不变，
                    # 但显式写入更稳（防止历史数据 backfill 默认值不一致）。
                    cur.execute(
                        """UPDATE listings
                           SET name=?, status=?, price_raw=?, available_from=?,
                               features=?, last_seen=?, last_status=?,
                               status_is_inferred=0, status_hold_until='', source=?
                           WHERE id=?""",
                        (
                            listing.name, listing.status, listing.price_raw,
                            listing.available_from,
                            json.dumps(listing.features, ensure_ascii=False),
                            now, listing.status, listing.source, listing.id,
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

    @staticmethod
    def _should_keep_booking_hold(
        old_row: dict | None,
        fresh_status: str,
        now: datetime,
    ) -> bool:
        if not old_row:
            return False
        if old_row.get("status") != "Reserved":
            return False
        if int(old_row.get("status_is_inferred") or 0) != 1:
            return False
        if fresh_status.lower() != "available to book":
            return False
        hold_until = _parse_iso(old_row.get("status_hold_until"))
        if hold_until is None:
            return False
        if hold_until.tzinfo is None:
            hold_until = hold_until.replace(tzinfo=timezone.utc)
        return hold_until > now

    def mark_listing_reserved_after_booking(self, listing_id: str) -> bool:
        """自动预订成功后，把本地状态暂时保持为 Reserved。"""
        now_dt = datetime.now(timezone.utc)
        hold_until = now_dt + timedelta(minutes=_booking_hold_minutes())
        with self._conn:
            cur = self._conn.execute(
                """UPDATE listings
                   SET status='Reserved',
                       last_status='Reserved',
                       status_is_inferred=1,
                       status_hold_until=?,
                       last_seen=?
                   WHERE id=?""",
                (hold_until.isoformat(), now_dt.isoformat(), listing_id),
            )
        return bool(cur.rowcount)

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
            placeholders = ",".join("?" for _ in listing_ids)
            self._conn.execute(
                f"""UPDATE status_changes SET notified=1
                   WHERE listing_id IN ({placeholders}) AND notified=0""",
                listing_ids,
            )

    # ── 状态收敛：last_seen 老化兜底 ─────────────────────────────────
    # 当前抓取源只返回 Available to book / lottery 子集；一旦 listing 转入
    # Reserved/Occupied，API 不再返回，DB 里就会永远停在 "Available" → 鬼影。
    #
    # 时间窗兜底：listing 已经 N 天没有刷新 last_seen，几乎可以肯定不再可订，
    # 直接标为 ``Occupied`` 让 UI/统计自然处理；同时 ``status_is_inferred=1``
    # 留下"这是推测值"的标记，供 Phase 3 的鬼影回归检测使用。
    #
    # Lottery 的生命周期通常更短：从完整扫描结果里消失后，大概率已经被
    # reserve/occupy，因此使用独立、更短的缺席窗口。
    #
    # 不写 status_changes：推测转换不触发通知/auto_book。Phase 3 引入
    # synthetic 列后才会写审计行。
    #
    # 仅作用于"看起来还可用"的 listing；已经 Occupied / 已 inferred 的不动。
    _STALE_GENERAL_STATUSES = (
        "Available to book",
        "Unknown",
    )
    _STALE_LOTTERY_STATUS = "Available in lottery"

    def mark_stale_listings(
        self,
        days: int = 7,
        cities: Optional[list[str]] = None,
        lottery_days: int = 2,
        source_city_pairs: Optional[list[tuple[str, str]]] = None,
    ) -> int:
        """
        把 `last_seen` 早于 cutoff 且状态仍是"看起来可用"的 listing
        标为 ``Occupied`` + ``status_is_inferred=1``。

        Parameters
        ----------
        days : book/unknown 老化阈值；默认 7 天（保守，避免误伤）
        cities : 限定当前仍在监控的城市；传入空列表时不更新任何 listing
        lottery_days : lottery 老化阈值；默认 2 天
        source_city_pairs : 限定 source + city 组合，用于多源同名城市隔离

        Returns
        -------
        本次实际更新的行数（已 Occupied / inferred=1 的不会被命中，幂等）
        """
        city_filter = [c for c in (cities or []) if c]
        source_city_filter = [
            (source, city)
            for source, city in (source_city_pairs or [])
            if source and city
        ]
        if (
            (cities is not None or source_city_pairs is not None)
            and not city_filter
            and not source_city_filter
        ):
            return 0

        now = datetime.now(timezone.utc)
        cutoff_general = (now - timedelta(days=max(1, int(days)))).isoformat()
        cutoff_lottery = (now - timedelta(days=max(1, int(lottery_days)))).isoformat()
        general_placeholders = ",".join("?" * len(self._STALE_GENERAL_STATUSES))
        sql = (
            f"UPDATE listings "
            f"SET status='Occupied', last_status='Occupied', status_is_inferred=1 "
            f"WHERE status_is_inferred = 0 "
            f"AND ("
            f"(last_seen < ? AND status IN ({general_placeholders})) "
            f"OR (last_seen < ? AND status = ?)"
            f")"
        )
        params: list[str] = [
            cutoff_general,
            *self._STALE_GENERAL_STATUSES,
            cutoff_lottery,
            self._STALE_LOTTERY_STATUS,
        ]
        scope_clauses: list[str] = []
        scope_params: list[str] = []
        if city_filter:
            city_placeholders = ",".join("?" * len(city_filter))
            scope_clauses.append(f"city IN ({city_placeholders})")
            scope_params.extend(city_filter)
        if source_city_filter:
            pair_clause = " OR ".join("(source = ? AND city = ?)" for _ in source_city_filter)
            scope_clauses.append(f"({pair_clause})")
            for source, city in source_city_filter:
                scope_params.extend([source, city])
        if scope_clauses:
            sql += " AND (" + " OR ".join(scope_clauses) + ")"
            params.extend(scope_params)
        with self._conn:
            cur = self._conn.execute(sql, params)
        return cur.rowcount or 0

    # ── 基础查询 ────────────────────────────────────────────────────

    def get_distinct_cities(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT city FROM listings WHERE city != '' ORDER BY city"
        ).fetchall()
        return [r[0] for r in rows]

    def get_distinct_sources(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT source FROM listings WHERE source != '' ORDER BY source"
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
        source: Optional[str] = None,
        limit: int = 500,
    ) -> list[dict]:
        q = "SELECT * FROM listings WHERE 1=1"
        params: list = []
        if status:
            q += " AND status = ?"
            params.append(status)
        if search:
            # 同时匹配 `name`（地址）和 `features` 里 "Building: ..." 这一项的楼盘名。
            # features 是 JSON 数组形如 ["Type: Studio", "Building: The Docks", ...]，
            # LIKE '%Building: %<search>%' 受限在 building 条目附近，避免误命中
            # 其它特征里的同名字符串（如 Neighborhood）。SQLite LIKE 对 ASCII
            # 默认不区分大小写，跟原有 name LIKE 行为一致，无须 COLLATE。
            q += " AND (name LIKE ? OR features LIKE ?)"
            params.append(f"%{search}%")
            params.append(f"%Building: %{search}%")
        if city:
            q += " AND city = ?"
            params.append(city)
        if source:
            q += " AND source = ?"
            params.append(source)
        q += " ORDER BY first_seen DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._conn.execute(q, params).fetchall()]

    def get_recent_changes(
        self, hours: int = 48, city: Optional[str] = None
    ) -> list[dict]:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        if city:
            rows = self._conn.execute(
                """SELECT sc.*, l.name, l.url, l.price_raw, l.source
                   FROM status_changes sc
                   JOIN listings l ON l.id = sc.listing_id
                   WHERE sc.changed_at > ? AND l.city = ?
                   ORDER BY sc.changed_at DESC""",
                (since, city),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT sc.*, l.name, l.url, l.price_raw, l.source
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

    def count_by_status(
        self,
        city: Optional[str] = None,
    ) -> dict[str, int]:
        """Return {status_lower: count} for the dashboard filter chips."""
        if city:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) FROM listings WHERE city = ? "
                "GROUP BY status",
                (city,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) FROM listings GROUP BY status"
            ).fetchall()
        return {r[0].lower(): r[1] for r in rows}

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
