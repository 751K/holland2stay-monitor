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


def local_time(iso_str: str, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """UTC ISO 时间戳 → 配置时区（``TIMEZONE``）的可读时间。

    库里所有时间戳都存 UTC（``last_scrape_at`` / ``first_seen`` / ``round_at``），
    但页面必须按 ``TIMEZONE`` 显示——容器跑在 ``TZ=Europe/Amsterdam``，日志的
    asctime 就是那个时区。直接把 UTC 原文渲染出来，夏令时期间会和 ``/logs``
    差两小时，而这两处本来就是对着看的。

    解析不了就原样返回：这是展示层，不该因为一个脏时间戳把整页打崩。
    """
    if not iso_str or iso_str == "—":
        return "—"
    try:
        from zoneinfo import ZoneInfo

        from config import TIMEZONE
        dt = datetime.fromisoformat(str(iso_str).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ZoneInfo(TIMEZONE)).strftime(fmt)
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


def status_short(status: str) -> str:
    """
    长状态字符串 → 短标签，给胶囊显示用。

    Holland2Stay 原始状态名很啰嗦（"Available to book" / "Available in lottery"），
    胶囊宽度差异巨大。本过滤器把它们截短到 1 个词，配合 .badge-status 等宽 CSS
    让 4 种状态胶囊视觉上长度一致：

    - Available to book      → "Book"
    - Available in lottery   → "Lottery"
    - Reserved / In process  → "Reserved"
    - Occupied / Rented / …  → "Occupied"

    未知状态保持原样（用作 fallback，避免静默丢失信息）。
    """
    s = (status or "").strip().lower()
    if "book" in s:
        return "Book"
    if "lottery" in s:
        return "Lottery"
    if "reserved" in s or "in process" in s or "pending" in s:
        return "Reserved"
    if "occupied" in s or "rented" in s or "not available" in s:
        return "Occupied"
    return status or ""


class StatusCapsule:
    """一次 .lower() 同时产出标签文案 + CSS 类名，避免模板里调两次 filter。

    用法：模板里 ``{% set cap = l.status | status_capsule %}``，
    然后 ``{{ cap.label }}`` + ``badge-{{ cap.css }}``。
    """
    __slots__ = ("label", "css")

    def __init__(self, label: str, css: str) -> None:
        self.label = label
        self.css = css


def status_capsule(status: str) -> StatusCapsule:
    """status → (short_label, css_class)，一次 .lower() 完成。

    原来模板里每行至少调 status_short + status_badge 两个 filter，每个 filter
    都各自 .lower() 一次。N 行列表 = 2N 次 .lower()。这里归并成单次调用。
    """
    s = (status or "").strip().lower()
    if "book" in s:
        return StatusCapsule("Book", "book")
    if "lottery" in s:
        return StatusCapsule("Lottery", "lottery")
    if "reserved" in s or "in process" in s or "pending" in s:
        return StatusCapsule("Reserved", "reserved")
    if "occupied" in s or "rented" in s or "not available" in s:
        return StatusCapsule("Occupied", "secondary")
    return StatusCapsule(status or "", "secondary")


def status_badge(status: str) -> str:
    """房源状态字符串 → badge 颜色类名（CSS 里有对应的 .badge-{name} 定义）。

    - book        → 绿（success）        Available to book
    - lottery     → 橙（warning）        Available in lottery
    - reserved    → 蓝（info）           Reserved / In Process（过渡态）
    - secondary   → 灰（neutral）        Occupied / Rented / Not available（终态）
    """
    s = status.lower()
    if "book" in s:
        return "success"
    if "lottery" in s:
        return "warning"
    if "reserved" in s or "in process" in s or "pending" in s:
        return "reserved"
    return "secondary"


def source_label(source: str) -> str:
    """Source id → user-facing platform label."""
    mapping = {
        "holland2stay": "Holland2Stay",
        "ourdomain": "OurDomain",
        "ourcampus": "OurCampus",
        "xior": "Xior",
        "magis": "Magis",
        "studentexperience": "Student Experience",
    }
    return mapping.get((source or "").lower(), source or "Holland2Stay")


def source_short(source: str) -> str:
    """Source id → compact platform label for dense tables."""
    mapping = {
        "holland2stay": "H2S",
        "ourdomain": "OD",
        "ourcampus": "OC",
        "xior": "XR",
        "magis": "MG",
        "studentexperience": "SE",
    }
    return mapping.get((source or "").lower(), source_label(source))


#: 过滤条件摘要里最多列几项，超过就只报个数。
#:
#: 2026-08-25 反馈：有人勾了 28 个片区，模板把它们用 "/" 连成一个**没有空格的
#: 长串**，浏览器只能在连字符处断行，于是文字直接溢出卡片右缘（截图里
#: "Schalkwijk/Sphin" 断在卡片外面）。列全了也没人会在列表页逐个读，要看有编辑页。
#:
#: 取 4 而不是 3：H2S 的户型常态就是「1 / 2 / Loft (open bedroom area) / Studio」
#: 四项，阈值再低一档就会把这个日常情况也折叠成一个数字，白丢信息。
_SUMMARY_LIMIT = 4


def summarize_list(values, limit: int = _SUMMARY_LIMIT) -> str:
    """短列表原样列出，长列表只报个数。

    ``["a","b"]``           → ``"a / b"``
    ``28 个片区``            → ``"28 个"`` / ``"28 selected"``

    分隔符两侧留空格：这一行的溢出根源就是 ``"/".join(...)`` 造出的无空格长串，
    没有断行机会。留了空格之后即便阈值被调大，也只是多占几行而不会溢出。

    只报个数而不是「前 3 项 +25」，是 2026-08-25 定的：后者要心算才知道总数，
    用户看到第一反应就是「+25 是啥意思」。完整清单挂在模板的 ``title`` 上。

    ``limit <= 0`` 表示不折叠（列表页之外的地方复用时用得上）。
    """
    items = [str(v).strip() for v in (values or []) if str(v).strip()]
    if not items:
        return ""
    if limit and len(items) > limit:
        return f"{len(items)} 个" if get_lang() == "zh" else f"{len(items)} selected"
    return " / ".join(items)


def register(app: "Flask") -> None:
    """把上述过滤器/全局函数挂到 Flask app 的 Jinja 环境。"""
    app.add_template_filter(time_ago,       "time_ago")
    app.add_template_filter(local_time,     "local_time")
    app.add_template_filter(price_short,    "price_short")
    app.add_template_filter(parse_features, "parse_features")
    app.add_template_filter(source_label,    "source_label")
    app.add_template_filter(source_short,    "source_short")
    app.add_template_filter(status_short,    "status_short")
    app.add_template_filter(status_capsule,  "status_capsule")
    app.add_template_filter(summarize_list,  "summarize_list")
    app.add_template_global(status_badge,   "status_badge")
