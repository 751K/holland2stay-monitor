"""Dashboard aggregate metrics shared by HTML and JSON routes."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config import DATA_DIR, load_config
from app.process_ctrl import monitor_pid


SUPPORTED_SOURCES = ("holland2stay", "ourdomain", "xior")
_RUN_COUNT_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ .*本次抓取共 (?P<count>\d+) 条房源"
)


def _int_or_zero(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _count_new_between(st: Any, *, start: datetime, end: datetime, city: str | None) -> int:
    params: list[object] = [start.isoformat(), end.isoformat()]
    sql = "SELECT COUNT(*) FROM listings WHERE first_seen > ? AND first_seen <= ?"
    if city:
        sql += " AND city = ?"
        params.append(city)
    row = st.conn.execute(sql, params).fetchone()
    return int(row[0]) if row else 0


def _count_changes_between(st: Any, *, start: datetime, end: datetime, city: str | None) -> int:
    params: list[object] = [start.isoformat(), end.isoformat()]
    sql = """SELECT COUNT(*) FROM status_changes sc
             JOIN listings l ON l.id = sc.listing_id
             WHERE sc.changed_at > ? AND sc.changed_at <= ?"""
    if city:
        sql += " AND l.city = ?"
        params.append(city)
    row = st.conn.execute(sql, params).fetchone()
    return int(row[0]) if row else 0


def _delta_label(current: int, previous: int, *, zh: bool) -> str:
    suffix = "较昨日" if zh else "vs yesterday"
    if previous == 0:
        if current == 0:
            return f"0 {suffix}"
        return f"+{current} {suffix}"
    pct = round((current - previous) * 100 / previous)
    sign = "+" if pct > 0 else ""
    return f"{sign}{pct}% {suffix}"


def _avg_run_count(*, days: int = 7, fallback: int = 0) -> tuple[int, int]:
    log_path = Path(DATA_DIR) / "monitor.log"
    if not log_path.exists():
        return fallback, 0

    since = datetime.now() - timedelta(days=days)
    counts: list[int] = []
    try:
        text = log_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return fallback, 0

    for line in text.splitlines():
        m = _RUN_COUNT_RE.match(line)
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if ts >= since:
            counts.append(int(m.group("count")))

    if not counts:
        return fallback, 0
    return round(sum(counts) / len(counts)), len(counts)


def _uptime_percent_7d(pid: int | None, st: Any = None) -> int:
    """监控进程在过去 7 天(168h)内的运行时间百分比。

    数据来自 ``Storage.record_uptime_sample()`` 每轮记的"每小时存活样本"
    （持久化在与 listings 同一个 DB → 同一个 Docker volume，重启/重建不丢，
    且真实反映中途宕机）。无 DB / 无样本时返回 0。
    """
    if st is None:
        return 0
    try:
        return st.uptime_percent_7d()
    except Exception:
        return 0


def _configured_scope() -> dict[str, int]:
    try:
        cfg = load_config()
        enabled_sources = {s for s in cfg.sources if s in SUPPORTED_SOURCES}
        targets = {task.city_display for task in cfg.scrape_tasks_v2()}
    except Exception:
        enabled_sources = set()
        targets = set()
    return {
        "enabled_platforms": len(enabled_sources),
        "supported_platforms": len(SUPPORTED_SOURCES),
        "configured_targets": len(targets),
    }


def dashboard_metrics(st: Any, *, city: str | None = None, lang: str = "en") -> dict:
    """Return aggregate counters for the dashboard cards."""
    now = datetime.now(timezone.utc)
    last_24h_start = now - timedelta(hours=24)
    prev_24h_start = now - timedelta(hours=48)
    zh = lang == "zh"

    new_24h = _count_new_between(st, start=last_24h_start, end=now, city=city)
    new_prev = _count_new_between(st, start=prev_24h_start, end=last_24h_start, city=city)
    changes_24h = _count_changes_between(st, start=last_24h_start, end=now, city=city)
    changes_prev = _count_changes_between(st, start=prev_24h_start, end=last_24h_start, city=city)

    last_count = _int_or_zero(st.get_meta("last_scrape_count", default="0"))
    avg_count, run_count = _avg_run_count(days=7, fallback=last_count)
    pid = monitor_pid()
    scope = _configured_scope()

    return {
        "total": st.count_all(city=city),
        "new_24h": new_24h,
        "new_24h_delta_label": _delta_label(new_24h, new_prev, zh=zh),
        "changes_24h": changes_24h,
        "changes_24h_delta_label": _delta_label(changes_24h, changes_prev, zh=zh),
        "last_scrape": st.get_meta("last_scrape_at", default=""),
        "last_count": last_count,
        "items_per_run": avg_count,
        "items_per_run_label": (
            (f"{run_count} 轮平均" if zh else f"avg of {run_count} runs")
            if run_count
            else ("最近一轮" if zh else "last run")
        ),
        "uptime_pct_7d": _uptime_percent_7d(pid, st=st),
        "uptime_label": "最近 7 天" if zh else "last 7 days",
        "configured_targets": scope["configured_targets"],
        "enabled_platforms": scope["enabled_platforms"],
        "supported_platforms": scope["supported_platforms"],
    }
