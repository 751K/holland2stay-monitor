"""
storage.py — SQLite 持久化层
==============================
职责
----
1. 维护 `listings` 表：记录历史上见过的所有房源快照（全量写入）
2. 维护 `status_changes` 表：记录每次状态变更事件
3. 维护 `meta` 表：键值存储，供面板显示最近抓取时间等运行时状态
4. `diff()` 是核心方法：对比本次抓取结果与库中现有状态，返回需要通知的事件
5. 提供 Web 面板所需的各类聚合查询

数据库 Schema
-------------
listings
    id              TEXT PRIMARY KEY   -- URL slug，e.g. "kastanjelaan-1-108"
    name            TEXT               -- 展示名
    status          TEXT               -- 当前可用性状态
    price_raw       TEXT               -- 原始价格字符串
    available_from  TEXT               -- 入住日期 YYYY-MM-DD
    features        TEXT               -- JSON 数组，["Type: Studio", ...]
    url             TEXT               -- 房源详情页 URL
    city            TEXT               -- 城市名
    first_seen      TEXT               -- UTC ISO 时间戳，首次入库时间
    last_seen       TEXT               -- UTC ISO 时间戳，最近一次抓到的时间
    notified        INTEGER DEFAULT 0  -- 是否已发送新房源通知（0/1）
    last_status     TEXT               -- 上一次已知状态，用于检测变更

status_changes
    id          INTEGER PRIMARY KEY AUTOINCREMENT
    listing_id  TEXT    -- 关联 listings.id
    old_status  TEXT
    new_status  TEXT
    changed_at  TEXT    -- UTC ISO 时间戳
    notified    INTEGER DEFAULT 0

meta
    key     TEXT PRIMARY KEY
    value   TEXT

web_notifications
    id          INTEGER PRIMARY KEY AUTOINCREMENT
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    type        TEXT NOT NULL              -- "new_listing" | "status_change" | "heartbeat" | "booking" | "error"
    title       TEXT NOT NULL
    body        TEXT NOT NULL DEFAULT ''
    url         TEXT NOT NULL DEFAULT ''   -- 房源详情页 URL（可为空）
    listing_id  TEXT NOT NULL DEFAULT ''   -- 关联 listings.id（可为空）
    read        INTEGER NOT NULL DEFAULT 0

常用 meta key
-------------
"last_scrape_at"    → 最近一次抓取完成的 UTC ISO 时间
"last_scrape_count" → 最近一次抓取到的房源总数

线程安全
--------
`Storage` 实例假定在单线程（asyncio 事件循环）中使用。
`check_same_thread=False` 仅为允许在 executor 线程中构造实例，
不意味着多线程并发安全。web.py 每个请求独立创建 Storage 实例。

依赖
----
仅标准库 + models.Listing，无其他内部依赖。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

from models import Listing, parse_features_list, parse_float

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    """返回当前 UTC 时间的 ISO 8601 字符串，用于 first_seen / last_seen / changed_at。"""
    return datetime.now(timezone.utc).isoformat()


class Storage:
    """
    SQLite 持久化层，封装所有数据库操作。

    生命周期
    --------
    monitor.py 在进程启动时创建一个实例，进程退出时调用 `close()`。
    web.py 在每个请求中按需创建独立实例（只读查询）。

    使用示例
    --------
    ::

        storage = Storage(Path("data/listings.db"))
        new_listings, status_changes = storage.diff(scraped_listings)
        for listing in new_listings:
            # 发送通知...
            storage.mark_notified(listing.id)
        storage.close()
    """

    def __init__(self, db_path: Path, timezone_str: str = "UTC") -> None:
        """
        打开（或创建）SQLite 数据库，自动执行 schema 迁移。

        Parameters
        ----------
        db_path      : 数据库文件路径，父目录不存在时自动创建
        timezone_str : IANA 时区标识符，用于图表日期分组（默认 UTC）。
                       部署在 Docker（UTC 时区）时应设为 Europe/Amsterdam
                       以确保图表按荷兰本地时间划分天边界。
        """
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._tz = timezone_str
        self._migrate()
        logger.debug("Storage 已连接: %s", db_path)

    # ------------------------------------------------------------------ #
    # Schema 管理
    # ------------------------------------------------------------------ #

    def _migrate(self) -> None:
        """
        执行幂等的 schema 初始化/迁移。
        使用 `CREATE TABLE IF NOT EXISTS` 保证多次调用安全。
        未来需要修改 schema 时在此处添加 ALTER TABLE 语句。
        """
        cur = self._conn.cursor()
        cur.executescript("""
            PRAGMA journal_mode=WAL;

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

            CREATE INDEX IF NOT EXISTS idx_web_notif_created
                ON web_notifications (created_at DESC);

            CREATE TABLE IF NOT EXISTS geocode_cache (
                address TEXT PRIMARY KEY,
                lat     REAL NOT NULL,
                lng     REAL NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            );
        """)
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # 核心：diff
    # ------------------------------------------------------------------ #

    def diff(
        self, fresh: list[Listing]
    ) -> tuple[list[Listing], list[tuple[Listing, str, str]]]:
        """
        将本次抓取结果与数据库对比，识别新房源和状态变更。

        处理逻辑
        --------
        对 `fresh` 中的每条房源：
        - 若 id 不在库中 → 插入新记录，加入 new_listings
        - 若 id 已存在   → 更新快照字段（name/status/price/features/last_seen）
                          若 status 与库中 last_status 不同 → 记录变更，加入 status_changes

        副作用
        ------
        - 将 fresh 中所有房源写入/更新 listings 表（全量 upsert）
        - 新增状态变更记录到 status_changes 表
        - 所有写操作在单个事务中原子提交

        Parameters
        ----------
        fresh : 本次抓取到的 Listing 列表（来自 scraper.scrape_all()）

        Returns
        -------
        new_listings   : 本轮全新发现的房源列表（notified=0，待通知）
        status_changes : [(listing, old_status, new_status), ...]
                         列表中的 listing 对象已含最新状态

        注意
        ----
        - 已在库但本次未抓到的房源不会被删除（历史保留）
        - 通知回执需调用方在发送成功后单独调用 mark_notified() / mark_status_change_notified()
        - `with self._conn:` 保证原子性：正常退出自动 COMMIT，异常自动 ROLLBACK。
          若不使用显式事务块，Python sqlite3 的隐式事务在 DML 语句触发时开始，
          异常路径不会主动 ROLLBACK，导致下次 diff() 的 DML 被追加到前一次
          未关闭的事务中，commit() 时合并两轮数据，产生重复通知。
        """
        now = _now_iso()
        new_listings: list[Listing] = []
        status_changes: list[tuple[Listing, str, str]] = []

        cur = self._conn.cursor()

        # with self._conn 在正常退出时调用 commit()，异常时调用 rollback()，
        # 确保本轮所有 INSERT/UPDATE 要么全部落库，要么全部回滚，不留半更新状态。
        with self._conn:
            # 批量查询所有已有记录，避免 N+1
            ids = [l.id for l in fresh]
            existing: dict[str, str] = {}  # listing_id → status
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

    # ------------------------------------------------------------------ #
    # 通知回执
    # ------------------------------------------------------------------ #

    def mark_notified(self, listing_id: str) -> None:
        """
        标记指定房源的新房源通知已发送。

        Parameters
        ----------
        listing_id : listings.id（URL slug）

        副作用
        ------
        设置 listings.notified = 1 并立即 commit。
        """
        with self._conn:
            self._conn.execute(
                "UPDATE listings SET notified=1 WHERE id=?", (listing_id,)
            )

    def mark_notified_batch(self, listing_ids: list[str]) -> None:
        """
        批量标记新房源通知已发送，单次 commit。
        在通知循环末尾调用，避免逐条 fsync。
        """
        if not listing_ids:
            return
        with self._conn:
            for lid in listing_ids:
                self._conn.execute(
                    "UPDATE listings SET notified=1 WHERE id=?", (lid,)
                )

    def mark_status_change_notified(self, listing_id: str) -> None:
        """
        标记指定房源所有未通知的状态变更记录为已通知。

        Parameters
        ----------
        listing_id : status_changes.listing_id（同 listings.id）

        副作用
        ------
        批量更新该 listing_id 下 notified=0 的记录为 notified=1，立即 commit。
        """
        with self._conn:
            self._conn.execute(
                """UPDATE status_changes SET notified=1
                   WHERE listing_id=? AND notified=0""",
                (listing_id,),
            )

    def mark_status_change_notified_batch(self, listing_ids: list[str]) -> None:
        """
        批量标记状态变更通知已发送，单次 commit。
        """
        if not listing_ids:
            return
        with self._conn:
            for lid in listing_ids:
                self._conn.execute(
                    """UPDATE status_changes SET notified=1
                       WHERE listing_id=? AND notified=0""",
                    (lid,),
                )

    # ------------------------------------------------------------------ #
    # 基础查询（monitor.py 内部使用）
    # ------------------------------------------------------------------ #

    def count_all(self) -> int:
        """返回 listings 表总行数（数据库中见过的房源总数）。"""
        row = self._conn.execute("SELECT COUNT(*) FROM listings").fetchone()
        return row[0] if row else 0

    def get_listing(self, listing_id: str) -> Optional[dict]:
        """
        按 id 查询单条房源。

        Returns
        -------
        dict（含所有字段）或 None（不存在时）
        """
        row = self._conn.execute(
            "SELECT * FROM listings WHERE id=?", (listing_id,)
        ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------ #
    # Web 面板查询
    # ------------------------------------------------------------------ #

    def get_all_listings(
        self,
        status: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 500,
    ) -> list[dict]:
        """
        查询房源列表，支持状态筛选和关键词搜索，供 Web 面板房源页使用。

        Parameters
        ----------
        status : 精确匹配 listings.status，None 表示不限
        search : 在 name 和 city 字段中做 LIKE 模糊匹配，None 表示不限
        limit  : 最多返回条数，默认 500

        Returns
        -------
        list[dict]，按 first_seen DESC 排序
        """
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
        """
        查询最近 N 小时内的状态变更记录，关联 listings 表获取房源名称。

        Parameters
        ----------
        hours : 时间窗口（小时），默认 48

        Returns
        -------
        list[dict]，含 status_changes 全部字段 + listings.name / url / price_raw，
        按 changed_at DESC 排序
        """
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
        """
        统计最近 N 小时内新入库的房源数量，供仪表盘「今日新增」指标使用。

        Parameters
        ----------
        hours : 时间窗口（小时），默认 24
        """
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        row = self._conn.execute(
            "SELECT COUNT(*) FROM listings WHERE first_seen > ?", (since,)
        ).fetchone()
        return row[0] if row else 0

    def count_changes_since(self, hours: int = 24) -> int:
        """
        统计最近 N 小时内的状态变更次数，供仪表盘「今日变更」指标使用。

        Parameters
        ----------
        hours : 时间窗口（小时），默认 24
        """
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        row = self._conn.execute(
            "SELECT COUNT(*) FROM status_changes WHERE changed_at > ?", (since,)
        ).fetchone()
        return row[0] if row else 0

    def get_calendar_listings(self) -> list[dict]:
        """
        查询所有含入住日期的房源，供日历视图渲染使用。

        Returns
        -------
        list[dict]，含 id / name / status / price_raw / available_from / url / city，
        按 available_from 升序排列，仅包含 available_from 非空的记录
        """
        rows = self._conn.execute(
            """SELECT id, name, status, price_raw, available_from, url, city
               FROM listings
               WHERE available_from IS NOT NULL AND available_from != ''
               ORDER BY available_from"""
        ).fetchall()
        return [
            {
                "id":             r["id"],
                "name":           r["name"],
                "status":         r["status"],
                "price_raw":      r["price_raw"] or "",
                "available_from": r["available_from"],
                "url":            r["url"] or "",
                "city":           r["city"] or "",
            }
            for r in rows
        ]

    # ── Geocode cache ──────────────────────────────────────────────── #

    def get_cached_coords(self, address: str) -> tuple[float, float] | None:
        """Return (lat, lng) from cache, or None."""
        row = self._conn.execute(
            "SELECT lat, lng FROM geocode_cache WHERE address = ?", (address,)
        ).fetchone()
        return (row["lat"], row["lng"]) if row else None

    def cache_coords(self, address: str, lat: float, lng: float) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO geocode_cache (address, lat, lng) VALUES (?, ?, ?)",
                (address, lat, lng),
            )

    def get_map_listings(self) -> list[dict]:
        """Return all listings with features for map display (geocoding done in route)."""
        rows = self._conn.execute(
            """SELECT id, name, status, price_raw, available_from, url, city, features
               FROM listings ORDER BY city, name LIMIT 2000"""
        ).fetchall()
        results: list[dict] = []
        for r in rows:
            feats = json.loads(r["features"] or "[]")
            feat_map = parse_features_list(feats)
            address = ", ".join(filter(None, [r["name"], r["city"] or "", feat_map.get("neighborhood", "")]))
            results.append({
                "id": r["id"],
                "name": r["name"],
                "status": r["status"],
                "price_raw": r["price_raw"] or "",
                "available_from": r["available_from"] or "",
                "url": r["url"] or "",
                "city": r["city"] or "",
                "neighborhood": feat_map.get("neighborhood", ""),
                "building": feat_map.get("building", ""),
                "area": feat_map.get("area", ""),
                "address": address,
            })
        return results

    def get_distinct_statuses(self) -> list[str]:
        """
        返回 listings 表中所有不重复的状态值，供面板过滤下拉菜单使用。
        """
        rows = self._conn.execute(
            "SELECT DISTINCT status FROM listings ORDER BY status"
        ).fetchall()
        return [r[0] for r in rows]

    # ------------------------------------------------------------------ #
    # Meta 键值存储
    # ------------------------------------------------------------------ #

    def get_meta(self, key: str, default: str = "—") -> str:
        """
        读取 meta 表中的键值。

        Parameters
        ----------
        key     : 元数据键，e.g. "last_scrape_at"
        default : 键不存在时的默认值

        Returns
        -------
        存储的字符串值，或 default
        """
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key=?", (key,)
        ).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        """
        写入 meta 表（UPSERT 语义）。

        Parameters
        ----------
        key   : 元数据键
        value : 字符串值
        """
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (key, value)
            )

    def load_retry_queue(self) -> dict[str, set[str]]:
        """
        从 meta 表恢复持久化的竞败重试队列。

        Returns
        -------
        dict[user_id, set[listing_id]]；无已保存队列时返回空 dict。

        说明
        ----
        JSON 不支持 set，存储格式为 dict[str, list[str]]，加载时转为 set。
        进程重启后队列不丢失，确保前一轮 race_lost 的房源在新一轮中继续重试。
        """
        raw = self.get_meta("retry_queue", "")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return {uid: set(lids) for uid, lids in data.items()}
        except Exception:
            logger.warning("retry_queue 数据损坏，已清除并重置为空")
            self.set_meta("retry_queue", "")
            return {}

    def save_retry_queue(self, queue: dict[str, set[str]]) -> None:
        """
        将竞败重试队列持久化到 meta 表。

        Parameters
        ----------
        queue : user_id → {listing_id, ...}，空 dict 会清除已存储的队列
        """
        if queue:
            data = {uid: list(lids) for uid, lids in queue.items()}
            self.set_meta("retry_queue", json.dumps(data, ensure_ascii=False))
        else:
            self.set_meta("retry_queue", "")

    # ------------------------------------------------------------------ #
    # 图表数据
    # ------------------------------------------------------------------ #

    def chart_daily_new(self, days: int = 30) -> list[dict]:
        """
        统计近 N 天每天新增房源数，供「新增趋势」折线图使用。

        Parameters
        ----------
        days : 统计天数，默认 30

        Returns
        -------
        list[{"date": "YYYY-MM-DD", "count": int}]，按日期升序，含所有日期（无数据的天 count=0）

        注意
        ----
        first_seen 存储为 UTC，日期分组使用构造时传入的 timezone_str
        （默认 Europe/Amsterdam），确保 Docker UTC 容器下天边界仍按荷兰本地时间对齐。
        """
        tz = ZoneInfo(self._tz)
        now_local = datetime.now(tz)
        today_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)

        # 起始日期（本地时间 midnight），包含该日
        start_local = today_local - timedelta(days=days)
        # WHERE 用 UTC 时间，加 1 天缓冲避免 DST 过渡日遗漏
        cutoff_utc = (start_local - timedelta(days=1)).isoformat()

        rows = self._conn.execute(
            "SELECT first_seen FROM listings WHERE first_seen >= ?",
            (cutoff_utc,),
        ).fetchall()

        # 按本地日期分组计数
        day_counts: dict[str, int] = {}
        for (ts,) in rows:
            utc_dt = datetime.fromisoformat(ts)
            local_date = utc_dt.astimezone(tz).strftime("%Y-%m-%d")
            day_counts[local_date] = day_counts.get(local_date, 0) + 1

        # 生成完整日期序列（旧→新），无数据天补零
        result: list[dict] = []
        for i in range(days, -1, -1):
            d = (today_local - timedelta(days=i)).strftime("%Y-%m-%d")
            result.append({"date": d, "count": day_counts.get(d, 0)})
        return result

    def chart_daily_changes(self, days: int = 30) -> list[dict]:
        """
        统计近 N 天每天状态变更次数，供「变更趋势」折线图使用。

        Parameters
        ----------
        days : 统计天数，默认 30

        Returns
        -------
        list[{"date": "YYYY-MM-DD", "count": int}]，按日期升序，含所有日期（无数据的天 count=0）

        注意
        ----
        changed_at 存储为 UTC，日期分组使用构造时传入的 timezone_str
        （默认 Europe/Amsterdam），确保 Docker UTC 容器下天边界仍按荷兰本地时间对齐。
        """
        tz = ZoneInfo(self._tz)
        now_local = datetime.now(tz)
        today_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)

        start_local = today_local - timedelta(days=days)
        cutoff_utc = (start_local - timedelta(days=1)).isoformat()

        rows = self._conn.execute(
            "SELECT changed_at FROM status_changes WHERE changed_at >= ?",
            (cutoff_utc,),
        ).fetchall()

        day_counts: dict[str, int] = {}
        for (ts,) in rows:
            utc_dt = datetime.fromisoformat(ts)
            local_date = utc_dt.astimezone(tz).strftime("%Y-%m-%d")
            day_counts[local_date] = day_counts.get(local_date, 0) + 1

        result: list[dict] = []
        for i in range(days, -1, -1):
            d = (today_local - timedelta(days=i)).strftime("%Y-%m-%d")
            result.append({"date": d, "count": day_counts.get(d, 0)})
        return result

    def chart_city_dist(self) -> list[dict]:
        """
        按城市统计当前库中所有房源数量，供「城市分布」饼图使用。

        Returns
        -------
        list[{"city": str, "count": int}]，按数量降序
        """
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
        """
        按状态统计当前库中所有房源数量，供「状态分布」饼图使用。

        Returns
        -------
        list[{"status": str, "count": int}]，按数量降序
        """
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
        """
        按租金区间统计房源数量，供「价格分布」柱状图使用。

        区间划分（固定）：<€600 / €600-700 / €700-800 / €800-900 / €900-1000 / >€1000

        Returns
        -------
        list[{"range": str, "count": int}]，按区间顺序排列（非降序）

        注意
        ----
        price_raw 在 Python 端解析，无法利用 SQLite 索引。数据量大时性能较差。
        """
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
            price = parse_float(raw)
            if price is None:
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

    # ------------------------------------------------------------------ #
    # Web 通知
    # ------------------------------------------------------------------ #

    def add_web_notification(
        self,
        *,
        type: str,
        title: str,
        body: str = "",
        url: str = "",
        listing_id: str = "",
    ) -> int:
        """
        写入一条 Web 通知记录。

        Parameters
        ----------
        type       : 通知类型，"new_listing" / "status_change" / "heartbeat" /
                     "booking" / "error"
        title      : 通知标题（短句，显示在铃铛弹框首行）
        body       : 详细文字（可为空）
        url        : 关联房源详情页 URL（可为空）
        listing_id : 关联 listings.id（可为空）

        Returns
        -------
        新记录的 id（整数）
        """
        with self._conn:
            cur = self._conn.execute(
                """INSERT INTO web_notifications (type, title, body, url, listing_id)
                   VALUES (?, ?, ?, ?, ?)""",
                (type, title, body, url, listing_id),
            )
            return cur.lastrowid  # type: ignore[return-value]

    def get_notifications(
        self,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """
        分页查询 Web 通知，按 created_at 倒序（最新在前）。

        Parameters
        ----------
        limit  : 每页条数
        offset : 跳过前 offset 条

        Returns
        -------
        list[dict]，含 web_notifications 表全部字段
        """
        rows = self._conn.execute(
            """SELECT * FROM web_notifications
               ORDER BY created_at DESC
               LIMIT ? OFFSET ?""",
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_notifications_since(self, last_id: int) -> list[dict]:
        """
        查询 id > last_id 的通知（SSE 增量推送用）。

        Parameters
        ----------
        last_id : 客户端已知的最大 id，传 0 表示拉取全部

        Returns
        -------
        list[dict]，按 id 升序（方便客户端按序处理）
        """
        rows = self._conn.execute(
            """SELECT * FROM web_notifications
               WHERE id > ?
               ORDER BY id ASC""",
            (last_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def count_unread_notifications(self) -> int:
        """返回未读通知数量，供铃铛角标使用。"""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM web_notifications WHERE read=0"
        ).fetchone()
        return row[0] if row else 0

    def mark_notifications_read(self, ids: list[int] | None = None) -> None:
        """
        标记通知为已读。

        Parameters
        ----------
        ids : 要标记的 id 列表；传 None 则标记全部未读通知
        """
        if ids is not None and not ids:
            return
        with self._conn:
            if ids is None:
                self._conn.execute(
                    "UPDATE web_notifications SET read=1 WHERE read=0"
                )
            else:
                placeholders = ",".join("?" * len(ids))
                self._conn.execute(
                    f"UPDATE web_notifications SET read=1 WHERE id IN ({placeholders})",
                    ids,
                )

    def prune_notifications(self, keep: int = 500) -> int:
        """
        保留最新 keep 条通知，删除多余的旧记录。

        Parameters
        ----------
        keep : 最多保留的记录数，默认 500

        Returns
        -------
        删除的行数
        """
        with self._conn:
            cur = self._conn.execute(
                """DELETE FROM web_notifications
                   WHERE id NOT IN (
                       SELECT id FROM web_notifications
                       ORDER BY id DESC
                       LIMIT ?
                   )""",
                (keep,),
            )
            return cur.rowcount

    def reset_all(self) -> None:
        """
        清空全部数据表（listings / status_changes / meta / web_notifications）。

        副作用
        ------
        在单个事务中 DELETE 四张表的所有行并立即 commit。
        不可逆操作，仅由 monitor.py 的 --reset-db 或交互式确认触发。
        """
        with self._conn:
            self._conn.execute("DELETE FROM listings")
            self._conn.execute("DELETE FROM status_changes")
            self._conn.execute("DELETE FROM meta")
            self._conn.execute("DELETE FROM web_notifications")
            self._conn.execute("DELETE FROM geocode_cache")
        logger.info("数据库已清空（listings / status_changes / meta / web_notifications / geocode_cache）")

    def close(self) -> None:
        """关闭数据库连接。进程退出时由 monitor.py 调用。"""
        self._conn.close()
