"""
Shared statistics service.

Web charts and API v1 public stats expose different HTTP shapes, but the
underlying chart keys, day-range normalization, and aggregate counts must stay
identical.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.services.listing_service import storage_ctx

DEFAULT_STATS_DAYS = 30
MIN_STATS_DAYS = 1
MAX_STATS_DAYS = 365

CHART_KEYS = (
    "daily_new",
    "daily_changes",
    "city_dist",
    "status_dist",
    "price_dist",
    "hourly_dist",
    "tenant_dist",
    "contract_dist",
    "type_dist",
    "energy_dist",
    "area_dist",
    "floor_dist",
)

ChartGetter = Callable[[Any, int], list[dict]]


def normalize_days(value: object, default: int = DEFAULT_STATS_DAYS) -> int:
    """Clamp a user-provided day range to the supported stats window."""
    try:
        days = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        days = default
    return max(MIN_STATS_DAYS, min(days, MAX_STATS_DAYS))


def _chart_getters() -> dict[str, ChartGetter]:
    return {
        "daily_new":     lambda st, days: st.chart_daily_new(days=days),
        "daily_changes": lambda st, days: st.chart_daily_changes(days=days),
        "city_dist":     lambda st, days: st.chart_city_dist(days=days),
        "status_dist":   lambda st, days: st.chart_status_dist(days=days),
        "price_dist":    lambda st, days: st.chart_price_dist(days=days),
        "hourly_dist":   lambda st, days: st.chart_hourly_dist(days=days),
        "tenant_dist":   lambda st, days: st.chart_tenant_dist(days=days),
        "contract_dist": lambda st, days: st.chart_contract_dist(days=days),
        "type_dist":     lambda st, days: st.chart_type_dist(days=days),
        "energy_dist":   lambda st, days: st.chart_energy_dist(days=days),
        "area_dist":     lambda st, days: st.chart_area_dist(days=days),
        "floor_dist":    lambda st, days: st.chart_floor_dist(days=days),
    }


def stats_summary(*, days: int = DEFAULT_STATS_DAYS) -> dict:
    """Shared aggregate counters for the selected range."""
    days = normalize_days(days)
    with storage_ctx() as st:
        return {
            "days": days,
            "total": st.count_all(),
            "new_24h": st.count_new_since(hours=24),
            "new_7d": st.count_new_since(hours=24 * 7),
            "new_range": st.count_new_since(hours=24 * days),
            "changes_24h": st.count_changes_since(hours=24),
            "changes_range": st.count_changes_since(hours=24 * days),
            "last_scrape": st.get_meta("last_scrape_at", default=""),
        }


def chart_keys() -> list[str]:
    """Public chart key list in a stable order."""
    return sorted(CHART_KEYS)


def chart_data(key: str, *, days: int = DEFAULT_STATS_DAYS) -> list[dict]:
    """Return one chart's data or raise KeyError for an unknown key."""
    getters = _chart_getters()
    getter = getters[key]
    days = normalize_days(days)
    with storage_ctx() as st:
        return getter(st, days)


def charts_payload(*, days: int = DEFAULT_STATS_DAYS) -> dict:
    """Return the Web `/api/charts` payload shape."""
    days = normalize_days(days)
    getters = _chart_getters()
    with storage_ctx() as st:
        payload = {
            "summary": {
                "days": days,
                "total": st.count_all(),
                "new_24h": st.count_new_since(hours=24),
                "new_range": st.count_new_since(hours=24 * days),
                "changes_range": st.count_changes_since(hours=24 * days),
            },
        }
        for key in CHART_KEYS:
            payload[key] = getters[key](st, days)
    return payload


def public_summary_payload() -> dict:
    """Return the existing API v1 public summary shape."""
    summary = stats_summary(days=7)
    return {
        "total": summary["total"],
        "new_24h": summary["new_24h"],
        "new_7d": summary["new_7d"],
        "changes_24h": summary["changes_24h"],
        "last_scrape": summary["last_scrape"],
    }
