"""
users.py — 多用户配置管理
==========================
职责
----
- 定义 `UserConfig` dataclass，包含单个用户的全部配置：
  通知渠道凭证、房源过滤条件、自动预订配置
- 提供 `load_users` / `save_users` / `update_users` 兼容接口

存储格式
--------
运行时存储在 SQLite `user_configs` 表。
旧版 `data/users.json` 只作为一次性迁移输入；迁移状态由 SQLite `meta`
表中的 `users_storage_migrated_v1` 控制，迁移后会永久保留 `.bak` 备份。

与 config.py 的分工
--------------------
- config.py / .env  → 全局参数（轮询间隔、城市、数据库路径）
- users.py / SQLite user_configs → 每用户独立配置（通知、过滤、预订）

依赖
----
标准库 + config.py（ListingFilter, AutoBookConfig）+ app.db 延迟导入。
"""
from __future__ import annotations

import json
import logging
import shutil
import uuid
from datetime import datetime, timezone
from dataclasses import asdict, dataclass, field, fields as dc_fields
from typing import Callable, Optional, TypeVar

from config import AutoBookConfig, DATA_DIR, ListingFilter
from crypto import decrypt, encrypt

logger = logging.getLogger(__name__)

USERS_FILE = DATA_DIR / "users.json"
USERS_MIGRATION_META_KEY = "users_storage_migrated_v1"
DEFAULT_EMAIL_SMTP_PORT = 587
_T = TypeVar("_T")


# ------------------------------------------------------------------ #
# 数据模型
# ------------------------------------------------------------------ #

@dataclass
class UserConfig:
    """
    单个用户的全部配置。

    字段说明
    --------
    name                  : 用户显示名，仅用于日志和 Web 面板
    id                    : 8 位十六进制 UUID 前缀，自动生成，作为内部唯一标识
    enabled               : False 时该用户的通知和自动预订全部跳过

    通知配置
    --------
    notifications_enabled : 该用户的通知总开关（独立于 enabled）
    notification_channels : 启用的渠道列表，支持 "imessage" / "telegram" / "whatsapp" / "email"
    imessage_recipient    : iMessage 收件人（手机号或 Apple ID 邮箱，macOS only）
    telegram_token        : Telegram Bot Token（格式 "123456789:AAB..."）
    telegram_chat_id      : Telegram Chat ID（数字字符串，向 bot 发消息后从 getUpdates 获取）
    email_smtp_host       : SMTP 主机（如 smtp.gmail.com）
    email_smtp_port       : SMTP 端口（常见：587 / 465）
    email_smtp_security   : SMTP 安全模式：starttls / ssl / none
    email_username        : SMTP 登录用户名（可选；若留空则不登录）
    email_password        : SMTP 登录密码 / App Password
    email_from            : 发件人邮箱
    email_to              : 收件人邮箱，支持逗号分隔多个地址
    twilio_sid            : Twilio Account SID（WhatsApp 渠道）
    twilio_token          : Twilio Auth Token
    twilio_from           : 发送方 WhatsApp 号码，格式 "whatsapp:+14155238886"
    twilio_to             : 接收方 WhatsApp 号码，格式 "whatsapp:+31612345678"

    过滤条件
    --------
    listing_filter        : 通知过滤条件，不满足的房源不发通知
                            is_empty() 时所有房源都通知

    自动预订
    --------
    auto_book             : 自动预订配置，enabled=False 时不触发预订
                            内含独立的 listing_filter（可比通知条件更严格）
    """

    name: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    enabled: bool = True

    notifications_enabled: bool = True
    notification_channels: list[str] = field(default_factory=list)
    imessage_recipient: str = ""
    telegram_token: str = ""
    telegram_chat_id: str = ""
    email_smtp_host: str = ""
    email_smtp_port: int = 587
    email_smtp_security: str = "starttls"
    email_username: str = ""
    email_password: str = ""
    email_from: str = ""
    email_to: str = ""
    twilio_sid: str = ""
    twilio_token: str = ""
    twilio_from: str = ""
    twilio_to: str = ""

    listing_filter: ListingFilter = field(default_factory=ListingFilter)
    auto_book: AutoBookConfig = field(default_factory=AutoBookConfig)

    # iOS / 第三方客户端登录凭证 -------------------------------------
    # 与 notification_channels 完全独立：这里只用于 App 登录鉴权，
    # 不影响通知/抓取/预订任何现有逻辑。
    #
    # app_password_hash : bcrypt(password).decode()；空字符串=未设置
    # app_login_enabled : False 时即使 hash 存在也拒绝 App 登录
    #                     默认 False（fail-closed，老用户不会意外打开 App 入口）
    # allow_h2s_login   : 是否允许 H2S 站点凭据作为 fallback 登录此本地账号。
    #                     默认 False（fail-closed）。开关意义：如果用户名恰好
    #                     等于其 H2S 邮箱、且 H2S 那边密码被泄露/撞库/钓鱼，
    #                     本字段为 False 时攻击者无法借用 H2S 凭据冒登本地账号。
    #                     仅当用户显式知情、信任该桥接时再开启。
    app_password_hash: str = ""
    app_login_enabled: bool = False
    allow_h2s_login: bool = False


# ------------------------------------------------------------------ #
# 内部：反序列化辅助
# ------------------------------------------------------------------ #

def _lf_from_dict(d: dict) -> ListingFilter:
    """从 dict 构造 ListingFilter，缺失字段使用默认值。"""
    return ListingFilter(
        max_rent=d.get("max_rent"),
        min_area=d.get("min_area"),
        min_floor=d.get("min_floor"),
        allowed_occupancy=d.get("allowed_occupancy", []),
        allowed_types=d.get("allowed_types", []),
        allowed_neighborhoods=d.get("allowed_neighborhoods", []),
        allowed_contract=d.get("allowed_contract", []),
        allowed_tenant=d.get("allowed_tenant", []),
        allowed_offer=d.get("allowed_offer", []),
        allowed_cities=d.get("allowed_cities", []),
        allowed_finishing=d.get("allowed_finishing", []),
        allowed_energy=d.get("allowed_energy", "") if isinstance(d.get("allowed_energy", ""), str) else "",
    )


def _ab_from_dict(d: dict) -> AutoBookConfig:
    """从 dict 构造 AutoBookConfig，内含 listing_filter 嵌套反序列化。"""
    return AutoBookConfig(
        enabled=d.get("enabled", False),
        dry_run=d.get("dry_run", True),
        email=d.get("email", ""),
        password=decrypt(d.get("password", "")),
        listing_filter=_lf_from_dict(d.get("listing_filter", {})),
        cancel_enabled=d.get("cancel_enabled", False),
        payment_method=d.get("payment_method", "idealcheckout_ideal"),
    )


def _user_from_dict(d: dict) -> UserConfig:
    """
    从 dict 构造 UserConfig，处理嵌套的 listing_filter 和 auto_book。
    兼容旧版本数据（缺失字段使用 dataclass 默认值）。

    未知字段处理
    ------------
    直接用 `UserConfig(**d)` 展开时，任何 UserConfig 不认识的 key 都会抛
    TypeError，导致整个 load_users() 失败（所有用户都无法加载）。

    此处先用 `dataclasses.fields()` 取出合法字段集合，剔除多余 key 并
    记录 WARNING，再展开剩余字段。典型场景：
    - 旧版 users.json 存有已被删除的字段
    - 未来版本新增字段后回滚到旧版代码
    - 手动编辑 users.json 时误加了多余 key
    """
    d = dict(d)
    lf = _lf_from_dict(d.pop("listing_filter", {}))
    ab = _ab_from_dict(d.pop("auto_book", {}))

    known   = {f.name for f in dc_fields(UserConfig)}
    unknown = set(d) - known
    if unknown:
        logger.warning(
            "用户 %r 包含未知字段，已忽略（可能来自旧版或新版 users.json）: %s",
            d.get("name", "?"), sorted(unknown),
        )
        d = {k: v for k, v in d.items() if k in known}

    # 解密敏感字段（无 $F$ 前缀的旧明文数据原样通过）
    for field in ("email_password", "telegram_token", "twilio_token"):
        if d.get(field):
            d[field] = decrypt(d[field])
    return UserConfig(**d, listing_filter=lf, auto_book=ab)


# ------------------------------------------------------------------ #
# 读写接口
# ------------------------------------------------------------------ #


def _open_storage():
    """延迟打开 Storage，避免 users.py import 阶段形成循环依赖。"""
    from app.db import storage

    return storage()


def _load_legacy_users_file() -> list[UserConfig]:
    """
    从旧版 `data/users.json` 加载用户列表，仅供一次性迁移使用。

    Returns
    -------
    list[UserConfig]
    - 文件不存在 → 返回空列表（首次运行，请在 Web 面板添加用户）
    - 文件存在且合法 → 返回解析结果（可能为空列表，表示有意清空）

    Raises
    ------
    RuntimeError
        文件存在但 JSON 解析失败（损坏/截断/写入中断）时抛出，
        而不是静默返回 []。调用方必须显式处理此异常，
        禁止在此情况下覆盖文件（会导致数据丢失）。
    """
    if not USERS_FILE.exists():
        return []
    try:
        data = json.loads(USERS_FILE.read_text(encoding="utf-8"))
        return [_user_from_dict(u) for u in data]
    except Exception as e:
        raise RuntimeError(
            f"{USERS_FILE} 存在但解析失败，请手动修复或从备份恢复后重启。\n"
            f"原因: {e}"
        ) from e


def load_users() -> list[UserConfig]:
    """从 SQLite `user_configs` 加载用户列表。"""
    st = _open_storage()
    try:
        _ensure_sqlite_users_migrated(st)
        return _rows_to_users(st.list_user_config_rows())
    finally:
        st.close()


def _user_to_row(u: UserConfig) -> dict:
    """UserConfig -> SQLite row dict。敏感字段按原 users.json 规则加密。"""
    from mstorage._user_configs import UserConfigOps

    d = asdict(u)
    for field_name in ("email_password", "telegram_token", "twilio_token"):
        if d.get(field_name):
            d[field_name] = encrypt(d[field_name])
    ab = d.get("auto_book", {})
    if ab.get("password"):
        ab["password"] = encrypt(ab["password"])

    return {
        "id": d["id"],
        "name": d["name"],
        "enabled": 1 if d.get("enabled") else 0,
        "notifications_enabled": 1 if d.get("notifications_enabled") else 0,
        "notification_channels_json": UserConfigOps.dumps_json(d.get("notification_channels", [])),
        "imessage_recipient": d.get("imessage_recipient", ""),
        "telegram_token": d.get("telegram_token", ""),
        "telegram_chat_id": d.get("telegram_chat_id", ""),
        "email_smtp_host": d.get("email_smtp_host", ""),
        "email_smtp_port": int(d.get("email_smtp_port") or DEFAULT_EMAIL_SMTP_PORT),
        "email_smtp_security": d.get("email_smtp_security", "starttls"),
        "email_username": d.get("email_username", ""),
        "email_password": d.get("email_password", ""),
        "email_from": d.get("email_from", ""),
        "email_to": d.get("email_to", ""),
        "twilio_sid": d.get("twilio_sid", ""),
        "twilio_token": d.get("twilio_token", ""),
        "twilio_from": d.get("twilio_from", ""),
        "twilio_to": d.get("twilio_to", ""),
        "listing_filter_json": UserConfigOps.dumps_json(d.get("listing_filter", {})),
        "auto_book_json": UserConfigOps.dumps_json(ab),
        "app_password_hash": d.get("app_password_hash", ""),
        "app_login_enabled": 1 if d.get("app_login_enabled") else 0,
        "allow_h2s_login": 1 if d.get("allow_h2s_login") else 0,
    }


def _row_to_user(row: dict) -> UserConfig:
    """SQLite row dict -> UserConfig。SQLite 字段固定，直接构造 dataclass。"""
    try:
        channels = json.loads(row.get("notification_channels_json") or "[]")
    except Exception:
        channels = []
    try:
        listing_filter = json.loads(row.get("listing_filter_json") or "{}")
    except Exception:
        listing_filter = {}
    try:
        auto_book = json.loads(row.get("auto_book_json") or "{}")
    except Exception:
        auto_book = {}

    email_password = row.get("email_password", "")
    telegram_token = row.get("telegram_token", "")
    twilio_token = row.get("twilio_token", "")
    auto_book_data = auto_book if isinstance(auto_book, dict) else {}

    if email_password:
        email_password = decrypt(email_password)
    if telegram_token:
        telegram_token = decrypt(telegram_token)
    if twilio_token:
        twilio_token = decrypt(twilio_token)
    ab = _ab_from_dict(auto_book_data)

    return UserConfig(
        id=row.get("id", ""),
        name=row.get("name", ""),
        enabled=bool(row.get("enabled")),
        notifications_enabled=bool(row.get("notifications_enabled")),
        notification_channels=channels if isinstance(channels, list) else [],
        imessage_recipient=row.get("imessage_recipient", ""),
        telegram_token=telegram_token,
        telegram_chat_id=row.get("telegram_chat_id", ""),
        email_smtp_host=row.get("email_smtp_host", ""),
        email_smtp_port=int(row.get("email_smtp_port") or DEFAULT_EMAIL_SMTP_PORT),
        email_smtp_security=row.get("email_smtp_security", "starttls"),
        email_username=row.get("email_username", ""),
        email_password=email_password,
        email_from=row.get("email_from", ""),
        email_to=row.get("email_to", ""),
        twilio_sid=row.get("twilio_sid", ""),
        twilio_token=twilio_token,
        twilio_from=row.get("twilio_from", ""),
        twilio_to=row.get("twilio_to", ""),
        listing_filter=_lf_from_dict(listing_filter if isinstance(listing_filter, dict) else {}),
        auto_book=ab,
        app_password_hash=row.get("app_password_hash", ""),
        app_login_enabled=bool(row.get("app_login_enabled")),
        allow_h2s_login=bool(row.get("allow_h2s_login")),
    )


def _users_to_rows(users: list[UserConfig]) -> list[dict]:
    return [_user_to_row(u) for u in users]


def _rows_to_users(rows: list[dict]) -> list[UserConfig]:
    return [_row_to_user(r) for r in rows]


def _backup_legacy_users_file() -> None:
    """永久保留 users.json 迁移备份；不覆盖旧备份。"""
    if not USERS_FILE.exists():
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = USERS_FILE.with_name(f"{USERS_FILE.name}.migrated.{stamp}.bak")
    target = base
    i = 1
    while target.exists():
        target = USERS_FILE.with_name(
            f"{USERS_FILE.name}.migrated.{stamp}.{i}.bak"
        )
        i += 1
    shutil.copy2(USERS_FILE, target)
    logger.info("users.json 已备份到 %s", target)


def _ensure_sqlite_users_migrated(st) -> None:
    """
    用 meta flag 控制 users.json -> SQLite 迁移。

    只要 meta flag 已设置，运行期永远只读 SQLite，即使 users.json 仍存在。
    """
    if st.get_meta(USERS_MIGRATION_META_KEY, default="") == "1":
        return

    conn = st.conn
    started = not conn.in_transaction
    if started:
        conn.execute("BEGIN IMMEDIATE")
    try:
        # 可能另一个进程刚刚迁移完成，拿到锁后再检查一次。
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?",
            (USERS_MIGRATION_META_KEY,),
        ).fetchone()
        if row and row[0] == "1":
            if started:
                conn.commit()
            return

        if USERS_FILE.exists():
            legacy_users = _load_legacy_users_file()
            _backup_legacy_users_file()
            st.replace_user_config_rows_unlocked(_users_to_rows(legacy_users))
            logger.info("已从 users.json 迁移 %d 个用户到 SQLite", len(legacy_users))

        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (USERS_MIGRATION_META_KEY, "1"),
        )
        if started:
            conn.commit()
    except Exception:
        if started:
            conn.rollback()
        raise


def save_users(users: list[UserConfig]) -> None:
    """将用户列表完整覆盖写入 SQLite `user_configs`。"""
    st = _open_storage()
    conn = st.conn
    try:
        conn.execute("BEGIN IMMEDIATE")
        _ensure_sqlite_users_migrated(st)
        st.replace_user_config_rows_unlocked(_users_to_rows(users))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        st.close()


def update_users(mutator: Callable[[list[UserConfig]], _T]) -> _T:
    """
    在 SQLite 事务内执行 read-modify-write。

    mutator 会收到最新用户列表，可以原地修改，并返回调用方需要的结果。
    只有 mutator 成功返回后才写回；抛异常时事务回滚。
    """
    st = _open_storage()
    conn = st.conn
    try:
        conn.execute("BEGIN IMMEDIATE")
        _ensure_sqlite_users_migrated(st)
        users = _rows_to_users(st.list_user_config_rows())
        result = mutator(users)
        st.replace_user_config_rows_unlocked(_users_to_rows(users))
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        st.close()


def get_user(users: list[UserConfig], user_id: str) -> Optional[UserConfig]:
    """
    在用户列表中按 id 查找单个用户。

    Parameters
    ----------
    users   : 已加载的用户列表
    user_id : UserConfig.id（8 位十六进制字符串）

    Returns
    -------
    对应的 UserConfig，不存在时返回 None
    """
    return next((u for u in users if u.id == user_id), None)


def get_user_by_name(
    users: list[UserConfig], name: str
) -> Optional[UserConfig]:
    """
    在用户列表中按 name 查找用户（精确匹配，大小写敏感）。

    App 端登录时用户输入 name + password，这里复用同一查找语义。
    name 不是唯一约束（UserConfig.id 才是），但 Web 后台编辑表单的
    保存路径校验过同名冲突，实际不会撞名。
    """
    return next((u for u in users if u.name == name), None)


# ------------------------------------------------------------------ #
# App 登录密码 —— bcrypt 包装
# ------------------------------------------------------------------ #
# 设计原则
# - 仅 UserConfig.app_password_hash 字段相关；不影响现有 email_password /
#   telegram_token 等业务凭证（这些字段是双向加密，要解密后传给第三方
#   API；密码是单向哈希，永远不需要解密）。
# - bcrypt 是 lazy import：只有调用 set/verify 的路径才需要装 bcrypt，
#   监控进程/抓取进程没必要为这层依赖买单（虽然实测开销很小）。
# - 失败永远 fail-closed：异常视为校验失败，不要泄漏 bcrypt 内部错误细节。


def _bcrypt_hash(plaintext: str) -> str:
    """纯哈希函数：bcrypt(plaintext) → ascii hash。空串返回空串。"""
    if not plaintext:
        return ""
    try:
        import bcrypt
    except ImportError:
        raise RuntimeError("bcrypt 未安装，无法设置 App 密码。请 pip install bcrypt。")
    return bcrypt.hashpw(
        plaintext.encode("utf-8"), bcrypt.gensalt()
    ).decode("ascii")


def set_app_password(user: UserConfig, plaintext: str) -> None:
    """
    给用户设置 App 登录密码。

    传空串视为"清除密码"（同时 app_login_enabled 不变，但 verify 必失败）。
    """
    user.app_password_hash = _bcrypt_hash(plaintext)


def verify_app_password(user: UserConfig, plaintext: str) -> bool:
    """
    校验 App 登录密码。

    返回 True 仅当：
    - user.app_login_enabled is True
    - user.app_password_hash 非空
    - bcrypt.checkpw 通过

    任何分支异常一律返回 False（fail-closed）。
    """
    if not user.app_login_enabled or not user.app_password_hash:
        return False
    if not plaintext:
        return False
    try:
        import bcrypt
        return bcrypt.checkpw(
            plaintext.encode("utf-8"),
            user.app_password_hash.encode("ascii"),
        )
    except Exception:
        logger.warning("verify_app_password 异常 (user=%s)", user.name)
        return False
