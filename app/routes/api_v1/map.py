"""
API v1 地图端点
================

GET /api/v1/map
    返回所有**已缓存坐标**的房源（不触发外部 Photon 请求）。
    user 视角应用 listing_filter；admin 视角全量。
"""

from __future__ import annotations

from flask import Blueprint

from app import api_auth, api_errors as _err
from app.services.listing_service import get_map_payload

from ._helpers import get_current_user


def _map():
    role = api_auth.current_role()
    user = get_current_user() if role == "user" else None
    if role == "user" and user is None:
        return _err.err_unauthorized("用户已被删除")

    return _err.ok(get_map_payload(user if role == "user" else None))


def register(bp: Blueprint) -> None:
    bp.add_url_rule(
        "/map",
        endpoint="map_list",
        view_func=api_auth.bearer_optional(_map),
        methods=["GET"],
    )
