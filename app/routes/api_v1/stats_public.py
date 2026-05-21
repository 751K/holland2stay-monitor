"""
API v1 公共统计端点（guest 可访问）
=====================================

设计原则
--------
- 这里的数据**不包含任何用户隔离信息**：只有"全库"级别的聚合统计。
  guest 看的图表与 admin 看的完全一致，不会泄漏单房源的 listing_id /
  细节字段。
- 仍走 ``@bearer_optional``：如果客户端带了 token 也接受（便于统一
  日志/限流），但不要求。
- 复用 ChartOps 的现成方法，零业务逻辑重复。
"""

from __future__ import annotations

from flask import Blueprint, request

from app import api_auth, api_errors as _err
from app.services.stats_service import (
    DEFAULT_STATS_DAYS,
    chart_data,
    chart_keys,
    normalize_days,
    public_summary_payload,
)


def _summary():
    """GET /stats/public/summary —— 首页几个总览数字。"""
    return _err.ok(public_summary_payload())


def _chart(key: str):
    """GET /stats/public/charts/<key>?days=30"""
    if key not in chart_keys():
        return _err.err_not_found(f"未知图表 {key!r}")
    days = normalize_days(request.args.get("days", DEFAULT_STATS_DAYS))
    data = chart_data(key, days=days)
    return _err.ok({"key": key, "days": days, "data": data})


def _charts_index():
    """GET /stats/public/charts —— 列出所有可用图表 key。"""
    return _err.ok({"charts": chart_keys()})


def register(bp: Blueprint) -> None:
    bp.add_url_rule(
        "/stats/public/summary",
        endpoint="stats_public_summary",
        view_func=api_auth.bearer_optional(_summary),
        methods=["GET"],
    )
    bp.add_url_rule(
        "/stats/public/charts",
        endpoint="stats_public_charts_index",
        view_func=api_auth.bearer_optional(_charts_index),
        methods=["GET"],
    )
    bp.add_url_rule(
        "/stats/public/charts/<string:key>",
        endpoint="stats_public_chart",
        view_func=api_auth.bearer_optional(_chart),
        methods=["GET"],
    )
