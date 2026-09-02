"""
API v1 地图端点
================

GET /api/v1/map
    返回所有**已缓存坐标**的房源（不触发外部 Photon 请求）。
    user 视角应用 listing_filter；admin 视角全量。

GET /api/v1/map/locate?id=<listing_id>
    按 id 定位单条，**绕过新鲜度窗口与用户筛选**。
    深链（房源详情 →「在地图上查看」）在「这个 id 不在已渲染集合里」时兜底。
"""

from __future__ import annotations

from flask import Blueprint, request

from app import api_auth, api_errors as _err
from app.services.listing_service import get_map_payload, locate_map_listing

from ._helpers import get_current_user


def _map():
    role = api_auth.current_role()
    user = get_current_user() if role == "user" else None
    if role == "user" and user is None:
        return _err.err_unauthorized("用户已被删除")

    return _err.ok(get_map_payload(user if role == "user" else None))


def _locate():
    """三种「看不到」分开报：not_found / no_coords / 有坐标但在视图之外。

    合并成一句「没找到」的话，「等管理员解析地址」「这个链接作废了」「改一下
    筛选就能看到」在界面上长得一模一样，而用户能做的事完全不同。
    """
    listing_id = (request.args.get("id") or "").strip()
    if not listing_id:
        return _err.err_validation("缺少 id")
    return _err.ok(locate_map_listing(listing_id))


def register(bp: Blueprint) -> None:
    bp.add_url_rule(
        "/map",
        endpoint="map_list",
        view_func=api_auth.bearer_optional(_map),
        methods=["GET"],
    )
    bp.add_url_rule(
        "/map/locate",
        endpoint="map_locate",
        view_func=api_auth.bearer_optional(_locate),
        methods=["GET"],
    )
