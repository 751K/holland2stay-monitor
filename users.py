"""
多用户管理模块
==============
每个用户独立拥有：通知渠道 + 凭证、房源过滤条件、自动预订账号。
全局配置（轮询间隔、城市、数据库路径等）仍在 config.py / .env 中管理。

存储格式：data/users.json（UTF-8 JSON 数组）
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from config import AutoBookConfig, ListingFilter

USERS_FILE = Path("data/users.json")


# ------------------------------------------------------------------ #
# 数据模型
# ------------------------------------------------------------------ #

@dataclass
class UserConfig:
    """单个用户的全部配置。"""

    name: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    enabled: bool = True

    # ── 通知 ──────────────────────────────────────────────────────── #
    notifications_enabled: bool = True
    notification_channels: list[str] = field(default_factory=list)
    # iMessage
    imessage_recipient: str = ""
    # Telegram
    telegram_token: str = ""
    telegram_chat_id: str = ""
    # WhatsApp（Twilio）
    twilio_sid: str = ""
    twilio_token: str = ""
    twilio_from: str = ""
    twilio_to: str = ""

    # ── 过滤条件（影响通知，不影响抓取/写库）──────────────────────── #
    listing_filter: ListingFilter = field(default_factory=ListingFilter)

    # ── 自动预订 ──────────────────────────────────────────────────── #
    auto_book: AutoBookConfig = field(default_factory=AutoBookConfig)


# ------------------------------------------------------------------ #
# 序列化 / 反序列化
# ------------------------------------------------------------------ #

def _lf_from_dict(d: dict) -> ListingFilter:
    return ListingFilter(
        max_rent=d.get("max_rent"),
        min_area=d.get("min_area"),
        max_area=d.get("max_area"),
        min_floor=d.get("min_floor"),
        allowed_occupancy=d.get("allowed_occupancy", []),
        allowed_types=d.get("allowed_types", []),
        allowed_neighborhoods=d.get("allowed_neighborhoods", []),
    )


def _ab_from_dict(d: dict) -> AutoBookConfig:
    return AutoBookConfig(
        enabled=d.get("enabled", False),
        dry_run=d.get("dry_run", True),
        email=d.get("email", ""),
        password=d.get("password", ""),
        listing_filter=_lf_from_dict(d.get("listing_filter", {})),
    )


def _user_from_dict(d: dict) -> UserConfig:
    d = dict(d)
    lf = _lf_from_dict(d.pop("listing_filter", {}))
    ab = _ab_from_dict(d.pop("auto_book", {}))
    return UserConfig(**d, listing_filter=lf, auto_book=ab)


# ------------------------------------------------------------------ #
# 读写
# ------------------------------------------------------------------ #

def load_users() -> list[UserConfig]:
    """从 data/users.json 加载用户列表。文件不存在时返回空列表。"""
    if not USERS_FILE.exists():
        return []
    try:
        data = json.loads(USERS_FILE.read_text(encoding="utf-8"))
        return [_user_from_dict(u) for u in data]
    except Exception:
        return []


def save_users(users: list[UserConfig]) -> None:
    """将用户列表序列化写入 data/users.json。"""
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    USERS_FILE.write_text(
        json.dumps([asdict(u) for u in users], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def get_user(users: list[UserConfig], user_id: str) -> Optional[UserConfig]:
    return next((u for u in users if u.id == user_id), None)


# ------------------------------------------------------------------ #
# 迁移：从旧 .env 单用户配置创建第一个用户
# ------------------------------------------------------------------ #

def migrate_from_env() -> Optional[UserConfig]:
    """
    读取旧版 .env 中的通知/过滤/预订配置，生成一个 "默认用户"。
    若 .env 没有任何通知配置，返回 None（用户自行在 Web 面板添加）。
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
        max_area=_f("MAX_AREA"),
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
            max_area=_f("AUTO_BOOK_MAX_AREA"),
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
        twilio_sid=os.environ.get("TWILIO_ACCOUNT_SID", ""),
        twilio_token=os.environ.get("TWILIO_AUTH_TOKEN", ""),
        twilio_from=os.environ.get("TWILIO_FROM", ""),
        twilio_to=os.environ.get("TWILIO_TO", ""),
        listing_filter=lf,
        auto_book=ab,
    )
