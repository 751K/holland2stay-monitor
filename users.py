"""
users.py — 多用户配置管理
==========================
职责
----
- 定义 `UserConfig` dataclass，包含单个用户的全部配置：
  通知渠道凭证、房源过滤条件、自动预订配置
- 提供 `data/users.json` 的读写接口
- 首次启动时从旧版 .env 单用户配置迁移（向后兼容）

存储格式
--------
`data/users.json`：JSON 数组，每个元素对应一个 UserConfig 的 asdict() 序列化结果。
文件不存在时视为无用户（空列表），首次写入时自动创建父目录。

与 config.py 的分工
--------------------
- config.py / .env  → 全局参数（轮询间隔、城市、数据库路径）
- users.py / users.json → 每用户独立配置（通知、过滤、预订）

依赖
----
标准库 + config.py（ListingFilter, AutoBookConfig），无其他内部依赖。
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field, fields as dc_fields
from pathlib import Path

from config import AutoBookConfig, DATA_DIR, ListingFilter
from crypto import decrypt, encrypt

logger = logging.getLogger(__name__)

USERS_FILE = DATA_DIR / "users.json"


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

def load_users() -> list[UserConfig]:
    """
    从 `data/users.json` 加载用户列表。

    Returns
    -------
    list[UserConfig]
    - 文件不存在 → 返回空列表（首次运行，调用方可执行 .env 迁移）
    - 文件存在且合法 → 返回解析结果（可能为空列表，表示有意清空）

    Raises
    ------
    RuntimeError
        文件存在但 JSON 解析失败（损坏/截断/写入中断）时抛出，
        而不是静默返回 []。调用方必须显式处理此异常，
        禁止在此情况下用迁移逻辑覆盖文件（会导致数据丢失）。
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


def save_users(users: list[UserConfig]) -> None:
    """
    将用户列表序列化写入 `data/users.json`（完整覆盖）。

    Parameters
    ----------
    users : 要持久化的用户列表，空列表会写入 "[]"

    副作用
    ------
    创建父目录（data/）如不存在；以原子方式覆盖已有文件。
    写入流程：先写 .tmp 临时文件，成功后用 os.replace() 原子替换目标文件。
    os.replace() 在同一文件系统上是原子操作（POSIX rename 语义），
    进程在写入中途被 kill 时只会丢失 .tmp，已有 users.json 不受影响。
    """
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = []
    for u in users:
        d = asdict(u)
        # 加密敏感字段后再持久化
        for field in ("email_password", "telegram_token", "twilio_token"):
            if d.get(field):
                d[field] = encrypt(d[field])
        ab = d.get("auto_book", {})
        if ab.get("password"):
            ab["password"] = encrypt(ab["password"])
        d["auto_book"] = ab
        data.append(d)
    tmp = USERS_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(tmp, USERS_FILE)


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


# ------------------------------------------------------------------ #
# 迁移：从旧版 .env 单用户配置创建默认用户
# ------------------------------------------------------------------ #

def migrate_from_env() -> Optional[UserConfig]:
    """
    读取旧版 .env 单用户配置，生成一个「默认用户」。

    触发条件
    --------
    仅在 `data/users.json` 不存在或为空时由 `monitor.py` 调用一次。
    若 .env 没有任何通知配置（IMESSAGE_RECIPIENT 和 NOTIFICATION_CHANNELS 均为空），
    返回 None，提示用户在 Web 面板手动添加。

    读取的 .env 键（完整列表）
    --------------------------
    通知：NOTIFICATION_CHANNELS, IMESSAGE_RECIPIENT,
          TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
          EMAIL_SMTP_HOST, EMAIL_SMTP_PORT, EMAIL_SMTP_SECURITY,
          EMAIL_USERNAME, EMAIL_PASSWORD, EMAIL_FROM, EMAIL_TO,
          TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM, TWILIO_TO,
          NOTIFICATIONS_ENABLED
    过滤：MAX_RENT, MIN_AREA, MIN_FLOOR,
          ALLOWED_OCCUPANCY, ALLOWED_TYPES, ALLOWED_NEIGHBORHOODS
    预订：AUTO_BOOK_ENABLED, AUTO_BOOK_DRY_RUN, AUTO_BOOK_EMAIL, AUTO_BOOK_PASSWORD,
          AUTO_BOOK_MAX_RENT, AUTO_BOOK_MIN_AREA, AUTO_BOOK_MIN_FLOOR,
          AUTO_BOOK_ALLOWED_OCCUPANCY, AUTO_BOOK_ALLOWED_TYPES, AUTO_BOOK_ALLOWED_NEIGHBORHOODS

    Returns
    -------
    UserConfig（name="默认用户"）或 None
    """
    import os

    channels_raw = os.environ.get("NOTIFICATION_CHANNELS", "")
    recipient    = os.environ.get("IMESSAGE_RECIPIENT", "")
    if not channels_raw and not recipient:
        return None

    channels = [c.strip().lower() for c in channels_raw.split(",") if c.strip()] or ["imessage"]

    def _f(k: str) -> Optional[float]:
        v = os.environ.get(k, "").strip()
        return float(v) if v else None

    def _i(k: str) -> Optional[int]:
        v = os.environ.get(k, "").strip()
        return int(v) if v else None

    def _l(k: str) -> list[str]:
        v = os.environ.get(k, "").strip()
        return [x.strip() for x in v.split(",") if x.strip()] if v else []

    lf = ListingFilter(
        max_rent=_f("MAX_RENT"),
        min_area=_f("MIN_AREA"),
        min_floor=_i("MIN_FLOOR"),
        allowed_occupancy=_l("ALLOWED_OCCUPANCY"),
        allowed_types=_l("ALLOWED_TYPES"),
        allowed_neighborhoods=_l("ALLOWED_NEIGHBORHOODS"),
    )
    ab = AutoBookConfig(
        enabled=os.environ.get("AUTO_BOOK_ENABLED", "false").lower() == "true",
        dry_run=os.environ.get("AUTO_BOOK_DRY_RUN", "true").lower() != "false",
        email=os.environ.get("AUTO_BOOK_EMAIL", ""),
        password=os.environ.get("AUTO_BOOK_PASSWORD", ""),
        listing_filter=ListingFilter(
            max_rent=_f("AUTO_BOOK_MAX_RENT"),
            min_area=_f("AUTO_BOOK_MIN_AREA"),
            min_floor=_i("AUTO_BOOK_MIN_FLOOR"),
            allowed_occupancy=_l("AUTO_BOOK_ALLOWED_OCCUPANCY"),
            allowed_types=_l("AUTO_BOOK_ALLOWED_TYPES"),
            allowed_neighborhoods=_l("AUTO_BOOK_ALLOWED_NEIGHBORHOODS"),
        ),
    )

    return UserConfig(
        name="默认用户",
        notifications_enabled=os.environ.get("NOTIFICATIONS_ENABLED", "true").lower() != "false",
        notification_channels=channels,
        imessage_recipient=recipient,
        telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
        email_smtp_host=os.environ.get("EMAIL_SMTP_HOST", ""),
        email_smtp_port=_i("EMAIL_SMTP_PORT") or 587,
        email_smtp_security=os.environ.get("EMAIL_SMTP_SECURITY", "starttls"),
        email_username=os.environ.get("EMAIL_USERNAME", ""),
        email_password=os.environ.get("EMAIL_PASSWORD", ""),
        email_from=os.environ.get("EMAIL_FROM", ""),
        email_to=os.environ.get("EMAIL_TO", ""),
        twilio_sid=os.environ.get("TWILIO_ACCOUNT_SID", ""),
        twilio_token=os.environ.get("TWILIO_AUTH_TOKEN", ""),
        twilio_from=os.environ.get("TWILIO_FROM", ""),
        twilio_to=os.environ.get("TWILIO_TO", ""),
        listing_filter=lf,
        auto_book=ab,
    )
