"""基础设施：连接管理、schema 迁移、meta 读写、生命周期。"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)


class StorageBase:
    """数据库连接 + schema + meta + 生命周期（其他 mixin 依赖此类提供 self._conn / self._tz）。"""

    # 进程级缓存：已迁移过 schema 的 db_path 集合。
    # CREATE TABLE IF NOT EXISTS + PRAGMA journal_mode=WAL 都是幂等的，
    # 但每次实例化（app/db.py 是"每请求一个 Storage"）都跑一次
    # executescript()，每请求多 ~3ms。同 db_path 在进程内只跑一次即可。
    _migrated_paths: set[str] = set()

    def __init__(self, db_path: Path, timezone_str: str = "UTC") -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._tz = timezone_str
        path_key = str(db_path.resolve())
        if path_key not in StorageBase._migrated_paths:
            self._migrate()
            StorageBase._migrated_paths.add(path_key)
        logger.debug("Storage 已连接: %s", db_path)

    # ── Public connection accessor ─────────────────────────────────

    @property
    def conn(self) -> sqlite3.Connection:
        """对外暴露底层 sqlite3 连接，供需要原生 SQL 的调用方使用
        （如 app/routes/system.py 的统计查询、测试 fixture）。

        Mixin 内部仍直接用 self._conn；这里只是稳定的外部 API，
        将来若把 _conn 改名/换成连接池，外部代码不会断。
        """
        return self._conn

    # ── Schema ──────────────────────────────────────────────────────

    def _migrate(self) -> None:
        # executescript() 会隐式 COMMIT 任何未决事务——必须在 __init__
        # 刚创建连接时立即调用，不能在已有未提交事务的连接上执行。
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
                read        INTEGER NOT NULL DEFAULT 0,
                user_id     TEXT NOT NULL DEFAULT ''
            );
            -- 关于 user_id 列上的索引：不能写在这里，因为对老库
            -- web_notifications 还没补字段（_add_column_if_missing 在
            -- executescript 之后跑）。索引创建在 _migrate 末尾完成。

            CREATE TABLE IF NOT EXISTS geocode_cache (
                address TEXT PRIMARY KEY,
                lat     REAL NOT NULL,
                lng     REAL NOT NULL
            );

            -- iOS / 第三方客户端的 Bearer 令牌（与 Web cookie session 独立）。
            -- token 明文只在签发时返回一次，库中只存 sha256(token)。
            -- role='admin' 时 user_id=NULL；role='user' 时 user_id 指向
            -- UserConfig.id（8 字符十六进制），便于 token 与用户隔离查询。
            CREATE TABLE IF NOT EXISTS app_tokens (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                token_hash   TEXT    UNIQUE NOT NULL,
                role         TEXT    NOT NULL,
                user_id      TEXT,
                device_name  TEXT    NOT NULL DEFAULT '',
                created_at   TEXT    NOT NULL,
                last_used_at TEXT,
                expires_at   TEXT,
                revoked      INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_app_tokens_user
                ON app_tokens(user_id, revoked);
            CREATE INDEX IF NOT EXISTS idx_app_tokens_active
                ON app_tokens(revoked, expires_at);

            -- iOS / 第三方客户端的 APNs 设备 token。
            -- 每次 App 启动会重新注册（token 可能轮换）；UNIQUE 约束
            -- (app_token_id, device_token) 保证同一会话内幂等。
            -- 关联到 app_tokens：会话撤销时设备自然失效（push.py 查询会 JOIN）。
            CREATE TABLE IF NOT EXISTS device_tokens (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                app_token_id    INTEGER NOT NULL,
                device_token    TEXT NOT NULL,
                env             TEXT NOT NULL DEFAULT 'production',
                platform        TEXT NOT NULL DEFAULT 'ios',
                model           TEXT NOT NULL DEFAULT '',
                bundle_id       TEXT NOT NULL DEFAULT '',
                created_at      TEXT NOT NULL,
                last_seen       TEXT NOT NULL,
                disabled_at     TEXT,
                disabled_reason TEXT,
                UNIQUE(app_token_id, device_token)
            );
            CREATE INDEX IF NOT EXISTS idx_device_tokens_active
                ON device_tokens(app_token_id, disabled_at);
        """)
        # 增量列：CREATE TABLE IF NOT EXISTS 不会给老库补字段；
        # ALTER TABLE ADD COLUMN 在 SQLite 上幂等需自己捕获 OperationalError。
        self._add_column_if_missing(
            "web_notifications", "user_id",
            "TEXT NOT NULL DEFAULT ''",
        )
        # 该列存在后再建索引（老库 / 新库都走到这一步时字段已就位）
        with self._conn:
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_web_notif_user "
                "ON web_notifications(user_id, id)"
            )

    def _add_column_if_missing(
        self, table: str, column: str, decl: str,
    ) -> None:
        """幂等 ALTER TABLE ADD COLUMN——已存在时静默跳过。"""
        # 先查 PRAGMA table_info 避免抛 OperationalError 进日志
        cols = {
            r[1] for r in self._conn.execute(
                f"PRAGMA table_info({table})"
            ).fetchall()
        }
        if column in cols:
            return
        with self._conn:
            self._conn.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {decl}"
            )
        logger.info("schema 升级: %s.%s 已添加", table, column)

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
            self._conn.execute("DELETE FROM app_tokens")
            self._conn.execute("DELETE FROM device_tokens")
        logger.info("数据库已清空（全部 7 张表）")

    def close(self) -> None:
        self._conn.close()
