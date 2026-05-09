"""
Jinja 模板过滤器与全局函数
============================

抽离自 web.py 顶层的 @app.template_filter / @app.template_global 注册块。
本模块提供纯函数实现，并通过 register(app) 一次性注册到 Flask app。

依赖
----
- app.i18n.get_lang（time_ago 的 zh/en 文案分支）
- models.parse_features_list（parse_features 的 JSON 反序列化）
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .i18n import get_lang

if TYPE_CHECKING:
    from flask import Flask


def time_ago(iso_str: str) -> str:
    """ISO 时间戳 → 相对时间文案（中/英根据当前语言）。"""
    if not iso_str or iso_str == "—":
        return "—"
    try:
        dt   = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        diff = datetime.now(timezone.utc) - dt
        secs = int(diff.total_seconds())
        zh   = get_lang() == "zh"
        if secs < 60:
            return f"{secs}秒前" if zh else f"{secs}s ago"
        if secs < 3600:
            m = secs // 60
            return f"{m}分钟前" if zh else f"{m}m ago"
        if secs < 86400:
            h = secs // 3600
            return f"{h}小时前" if zh else f"{h}h ago"
        d = secs // 86400
        return f"{d}天前" if zh else f"{d}d ago"
    except Exception:
        return iso_str


def price_short(price_raw: str) -> str:
    """从原始价格串中抽出第一段 €xxx 数字部分。"""
    if not price_raw:
        return "—"
    m = re.search(r"€[\d,\.]+", price_raw)
    return m.group() if m else price_raw


def parse_features(features_json: str) -> dict[str, str]:
    """房源 features JSON 串 → 字段字典（供模板按 key 取值）。"""
    from models import parse_features_list  # 局部 import：避免 app/ 包加载时强制依赖 models
    try:
        items = json.loads(features_json or "[]")
    except Exception:
        return {}
    return parse_features_list(items)


def status_badge(status: str) -> str:
    """房源状态字符串 → Bootstrap badge 颜色类名。"""
    s = status.lower()
    if "book" in s:
        return "success"
    if "lottery" in s:
        return "warning"
    return "secondary"


def register(app: "Flask") -> None:
    """把上述过滤器/全局函数挂到 Flask app 的 Jinja 环境。"""
    app.add_template_filter(time_ago,       "time_ago")
    app.add_template_filter(price_short,    "price_short")
    app.add_template_filter(parse_features, "parse_features")
    app.add_template_global(status_badge,   "status_badge")
