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

import json
import logging
from contextlib import contextmanager
from dataclasses import asdict
from typing import Iterable, Optional

from flask import g

from app.db import storage
from models import Listing
from users import UserConfig, load_users

logger = logging.getLogger(__name__)


# ── Storage 上下文管理器 ────────────────────────────────────────────


@contextmanager
def storage_ctx():
    """``with storage_ctx() as st:`` 自动 close。"""
    st = storage()
    try:
        yield st
    finally:
        st.close()


# ── 行 → Listing ────────────────────────────────────────────────────


def row_to_listing(row: dict) -> Listing:
    """
    SQLite 行 dict → models.Listing。

    Storage 写入时 ``features`` 是 JSON 字符串；这里反序列化。
    缺失的预订专用字段（sku / contract_id / contract_start_date）置默认。
    """
    raw = row.get("features", "[]") or "[]"
    try:
        feats = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("损坏的 features JSON (id=%s)", row.get("id"))
        feats = []
    return Listing(
        id=row.get("id", "") or "",
        name=row.get("name", "") or "",
        status=row.get("status", "") or "",
        price_raw=row.get("price_raw") or None,
        available_from=row.get("available_from") or None,
        features=feats if isinstance(feats, list) else [],
        url=row.get("url", "") or "",
        city=row.get("city", "") or "",
    )


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


# ── 过滤 ────────────────────────────────────────────────────────────


def apply_user_filter(
    rows: Iterable[dict],
    user: Optional[UserConfig],
) -> list[dict]:
    """
    把房源行列表按 user.listing_filter 过滤。

    user is None     → admin 视角，原样返回
    filter.is_empty  → 没设过滤条件，原样返回
    其他             → 逐条调 ListingFilter.passes()，留下匹配的
    """
    if user is None:
        return list(rows)
    f = user.listing_filter
    if f.is_empty():
        return list(rows)
    out: list[dict] = []
    for r in rows:
        try:
            if f.passes(row_to_listing(r)):
                out.append(r)
        except Exception:
            # 过滤器异常不应把整个请求干翻；记日志，跳过该行。
            logger.exception("apply_user_filter: 过滤异常 id=%s", r.get("id"))
    return out


# ── JSON 形状 ───────────────────────────────────────────────────────


def serialize_listing(row: dict) -> dict:
    """
    统一的房源响应 shape，避免不同端点字段不一致。

    展开 features JSON 为数组，方便 iOS 端直接遍历；
    price_value（数值）便于排序/过滤；保持 price_raw 原文供展示。
    """
    raw_features = row.get("features", "[]") or "[]"
    try:
        feats = json.loads(raw_features)
    except (json.JSONDecodeError, TypeError):
        feats = []
    from models import parse_float, parse_features_list
    fm = parse_features_list(feats) if isinstance(feats, list) else {}
    price_val = parse_float(row.get("price_raw", ""))
    return {
        "id": row.get("id", ""),
        "name": row.get("name", ""),
        "status": row.get("status", ""),
        "price_raw": row.get("price_raw") or "",
        "price_value": price_val,
        "available_from": row.get("available_from") or "",
        "city": row.get("city") or "",
        "url": row.get("url") or "",
        "features": feats if isinstance(feats, list) else [],
        "feature_map": fm,
        "first_seen": row.get("first_seen") or "",
        "last_seen": row.get("last_seen") or "",
    }


def serialize_filter(user: Optional[UserConfig]) -> dict:
    """把 user.listing_filter 转 JSON-safe dict；user=None 返回空 filter。"""
    if user is None:
        return {}
    return asdict(user.listing_filter)
