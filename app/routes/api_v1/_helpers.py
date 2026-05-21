"""
API v1 共享工具
================

集中放：
- row_to_listing      ：把 SQLite 行 dict 重组成 ``models.Listing`` 对象，
                        以便复用 ``ListingFilter.passes()`` 这套既有过滤逻辑。
- get_current_user    ：从 ``g.api_user_id`` 拿 UserConfig；admin/guest 返回 None。
- serialize_listing   ：统一的房源 JSON 形状，避免每个端点各自拼。
- apply_user_filter   ：admin 直通；user 走 listing_filter；guest 不该调到这里。
- with_storage        ：上下文管理器，免每个 view 写 try/finally。
"""

from __future__ import annotations

import logging
from typing import Optional

from flask import g

from app.services.listing_service import (
    apply_user_filter,
    row_to_listing,
    serialize_filter,
    serialize_listing,
    storage_ctx,
)
from users import UserConfig, load_users

logger = logging.getLogger(__name__)


# ── 身份解析 ────────────────────────────────────────────────────────


def get_current_user() -> Optional[UserConfig]:
    """
    role=user 时返回对应的 UserConfig；admin / guest / 未登录返回 None。

    被 ``bearer_required(("user",))`` 守门时，调用方可安全断言 None 即 404，
    因为只有合法 user token 才会到这里。
    """
    uid = getattr(g, "api_user_id", None)
    if not uid:
        return None
    try:
        users = load_users()
    except RuntimeError:
        logger.exception("load_users 失败")
        return None
    return next((u for u in users if u.id == uid), None)
