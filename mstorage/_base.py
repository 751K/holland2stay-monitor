"""基础设施：连接管理、schema 迁移、meta 读写、生命周期。"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


class StorageBase:
    """数据库连接 + schema + meta + 生命周期（其他 mixin 依赖此类提供 self._conn / self._tz）。"""

    # 进程级缓存：已迁移过 schema 的 db_path 集合。
    # CREATE TABLE IF NOT EXISTS + PRAGMA journal_mode=WAL 都是幂等的，
    # 但每次实例化（app/db.py 是"每请求一个 Storage"）都跑一次
    # executescript()，每请求多 ~3ms。同 db_path 在进程内只跑一次即可。
    _migrated_paths: set[str] = set()
    _migration_lock = threading.RLock()

    def __init__(self, db_path: Path, timezone_str: str = "UTC") -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._tz = timezone_str
        # _teardown_managed 为 True 时，close() 是空操作——连接的真正关闭由
        # Flask teardown_appcontext 负责（参见 app/db.py）。非请求上下文（monitor /
        # CLI / 测试）创建的实例此标志为 False，.close() 照常关闭连接。
        self._teardown_managed: bool = False
        path_key = str(db_path.resolve())
        if path_key not in StorageBase._migrated_paths:
            with StorageBase._migration_lock:
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
                id                TEXT PRIMARY KEY,
                name              TEXT,
                status            TEXT,
                price_raw         TEXT,
                available_from    TEXT,
                features          TEXT,
                url               TEXT,
                city              TEXT,
                first_seen        TEXT,
                last_seen         TEXT,
                notified          INTEGER DEFAULT 0,
                last_status       TEXT,
                -- status_is_inferred=1 表示 status 字段是系统推测的（如
                -- mark_stale_listings 把 7 天未刷新的 listing 标为 Occupied），
                -- 不是从 API 真实读到的。下次 API 真的返回该 listing 时，
                -- diff() 会把 inferred 复位为 0。Phase 3 的"鬼影回归"检测靠它。
                status_is_inferred INTEGER NOT NULL DEFAULT 0
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

            -- 用户完整配置。UserConfig.id 是稳定主键，来自旧 users.json
            -- 或新注册时的 UserConfig 默认 id，迁移时绝不重新生成。
            CREATE TABLE IF NOT EXISTS user_configs (
                id                         TEXT PRIMARY KEY,
                name                       TEXT UNIQUE NOT NULL,
                enabled                    INTEGER NOT NULL DEFAULT 1,
                notifications_enabled      INTEGER NOT NULL DEFAULT 1,
                notification_channels_json TEXT NOT NULL DEFAULT '[]',
                imessage_recipient         TEXT NOT NULL DEFAULT '',
                telegram_token             TEXT NOT NULL DEFAULT '',
                telegram_chat_id           TEXT NOT NULL DEFAULT '',
                email_mode                 TEXT NOT NULL DEFAULT 'shared',
                email_smtp_host            TEXT NOT NULL DEFAULT '',
                email_smtp_port            INTEGER NOT NULL DEFAULT 587,
                email_smtp_security        TEXT NOT NULL DEFAULT 'starttls',
                email_username             TEXT NOT NULL DEFAULT '',
                email_password             TEXT NOT NULL DEFAULT '',
                email_from                 TEXT NOT NULL DEFAULT '',
                email_to                   TEXT NOT NULL DEFAULT '',
                twilio_sid                 TEXT NOT NULL DEFAULT '',
                twilio_token               TEXT NOT NULL DEFAULT '',
                twilio_from                TEXT NOT NULL DEFAULT '',
                twilio_to                  TEXT NOT NULL DEFAULT '',
                listing_filter_json        TEXT NOT NULL DEFAULT '{}',
                auto_book_json             TEXT NOT NULL DEFAULT '{}',
                app_password_hash          TEXT NOT NULL DEFAULT '',
                app_login_enabled          INTEGER NOT NULL DEFAULT 0,
                allow_h2s_login            INTEGER NOT NULL DEFAULT 0,
                sort_order                 INTEGER NOT NULL DEFAULT 0,
                language                   TEXT NOT NULL DEFAULT 'en',
                created_at                 TEXT NOT NULL,
                updated_at                 TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_user_configs_name
                ON user_configs(name);

            -- app_users 曾经只是 users.json 的账号镜像。UserConfig 完整迁入
            -- SQLite 后，单独账号表会制造双源状态，按 v1 迁移策略直接移除。
            DROP TABLE IF EXISTS app_users;

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
                language        TEXT NOT NULL DEFAULT 'en',
                UNIQUE(app_token_id, device_token)
            );
            CREATE INDEX IF NOT EXISTS idx_device_tokens_active
                ON device_tokens(app_token_id, disabled_at);

            -- 收件邮箱归属验证（防 shared 模式被当成代发服务滥用）。
            -- 仅 email_mode='shared' 时强制使用；custom 模式用户自负责。
            -- 同一邮箱可对应多个 token（用户多次点重发），最新未过期的有效。
            CREATE TABLE IF NOT EXISTS email_verifications (
                token       TEXT PRIMARY KEY,
                user_id     TEXT NOT NULL,
                email       TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                expires_at  TEXT NOT NULL,
                verified_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_email_verif_user
                ON email_verifications(user_id, email);

            -- Resend 每日配额计数。多 Gunicorn worker 共享同一 SQLite，
            -- 避免内存计数器各 worker 独立导致实际上限 = limit × N。
            -- scope: 'global' (key='') 或 'user' (key=user_id)
            -- day  : UTC 日期 YYYY-MM-DD（按 UTC 切换避免本地时区跳变重置）
            -- 老旧行（>30 天）由 prune_old_email_send_counters 清理。
            CREATE TABLE IF NOT EXISTS email_send_counters (
                scope TEXT NOT NULL,
                key   TEXT NOT NULL,
                day   TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (scope, key, day)
            );
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

        # email_mode：shared (Resend) / custom (SMTP)；新库默认 shared。
        # 老库迁移：填过 email_smtp_host 的存量用户视为 custom，避免行为突变。
        added = self._add_column_if_missing(
            "user_configs", "email_mode",
            "TEXT NOT NULL DEFAULT 'shared'",
        )
        if added:
            with self._conn:
                self._conn.execute(
                    "UPDATE user_configs SET email_mode='custom' "
                    "WHERE email_smtp_host <> '' AND email_mode='shared'"
                )

        # email_verified：shared 模式下 email_to 必须经过 double opt-in。
        # 老库迁移：已有 email_to 的存量用户视为已验证（admin 信任前提，
        # 避免升级后存量用户邮件突然全失效）。新增/修改邮箱才触发验证流程。
        added_ev = self._add_column_if_missing(
            "user_configs", "email_verified",
            "INTEGER NOT NULL DEFAULT 0",
        )
        if added_ev:
            with self._conn:
                self._conn.execute(
                    "UPDATE user_configs SET email_verified=1 "
                    "WHERE email_to <> '' AND email_verified=0"
                )

        # language：用户推送语言偏好（en / zh），默认英文。
        self._add_column_if_missing(
            "user_configs", "language",
            "TEXT NOT NULL DEFAULT 'en'",
        )

        # status_is_inferred：标识 listing.status 是否由系统推测产生。
        # 老库默认 0（都是 API 真实数据），mark_stale_listings 触发时才置 1。
        self._add_column_if_missing(
            "listings", "status_is_inferred",
            "INTEGER NOT NULL DEFAULT 0",
        )
        # 自动预订成功后的本地状态保持窗口。hold 期内如果 scraper 仍返回
        # Available to book，不立即覆盖本地 Reserved，避免下一轮重复预订。
        self._add_column_if_missing(
            "listings", "status_hold_until",
            "TEXT NOT NULL DEFAULT ''",
        )

        # P0 多源重构：listings.source 标识房源来自哪个第三方平台。
        # 老库默认 'holland2stay'（迁移时全量房源都属于 H2S，符合实际）；
        # 新写入由 ListingOps.diff() 从 Listing.source 字段读取。
        # 索引：跨 source 时按 source / (source, city) 过滤的查询会很多。
        self._add_column_if_missing(
            "listings", "source",
            "TEXT NOT NULL DEFAULT 'holland2stay'",
        )
        with self._conn:
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_listings_source "
                "ON listings(source)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_listings_source_city "
                "ON listings(source, city)"
            )
            # 高频查询列索引：city（首页城市筛选）、first_seen（ORDER BY + 排序）、
            # status（状态筛选）、last_seen（老化检测 + 排序）。
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_listings_city "
                "ON listings(city)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_listings_first_seen "
                "ON listings(first_seen)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_listings_status "
                "ON listings(status)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_listings_last_seen "
                "ON listings(last_seen)"
            )
            # status_changes 高频查询列索引
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_status_changes_changed_at "
                "ON status_changes(changed_at)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_status_changes_listing_id "
                "ON status_changes(listing_id)"
            )

        # device_tokens.language：APNs 推送语言（'en' | 'zh'）。
        # 客户端注册设备时上报；老设备默认 'en'。
        self._add_column_if_missing(
            "device_tokens", "language",
            "TEXT NOT NULL DEFAULT 'en'",
        )

    def _add_column_if_missing(
        self, table: str, column: str, decl: str,
    ) -> bool:
        """
        幂等 ALTER TABLE ADD COLUMN——已存在时静默跳过。

        Returns
        -------
        True 表示本次调用真的执行了 ADD COLUMN（首次部署 / 升级），
        False 表示字段早已存在（既有库，无操作）。
        调用方可据此触发一次性数据迁移（如 backfill 默认值）。
        """
        # 先查 PRAGMA table_info 避免抛 OperationalError 进日志
        cols = {
            r[1] for r in self._conn.execute(
                f"PRAGMA table_info({table})"
            ).fetchall()
        }
        if column in cols:
            return False
        try:
            with self._conn:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {decl}"
                )
        except sqlite3.OperationalError as e:
            # 多线程/多 worker 首次启动时可能在 PRAGMA 与 ALTER 之间被
            # 另一个连接抢先完成迁移；SQLite 报 duplicate column 时视为
            # 幂等成功后的 no-op，避免并发注册/写入时偶发 500。
            if "duplicate column name" in str(e).lower():
                return False
            raise
        logger.info("schema 升级: %s.%s 已添加", table, column)
        return True

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
            self._conn.execute("DELETE FROM user_configs")
            self._conn.execute("DELETE FROM device_tokens")
        logger.info("数据库已清空（全部 8 张表）")

    def close(self) -> None:
        if not self._teardown_managed:
            self._conn.close()
