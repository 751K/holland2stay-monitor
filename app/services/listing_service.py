"""
Shared listing read/query service.

This module keeps data access, feature parsing, and user listing_filter
application out of route handlers. Web routes and API v1 routes can keep their
own auth and response envelopes while sharing the same listing behavior.
"""
from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from dataclasses import asdict
from typing import Any, Iterable, Optional

from app.db import storage
from models import Listing
from users import UserConfig

logger = logging.getLogger(__name__)


@contextmanager
def storage_ctx():
    """Yield a storage instance and always close it."""
    st = storage()
    try:
        yield st
    finally:
        st.close()


def safe_features(row: dict) -> list[str]:
    """Parse a listings.features JSON string safely."""
    raw = row.get("features", "[]") or "[]"
    try:
        feats = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("损坏的 features JSON (id=%s): %.80s", row.get("id"), raw)
        return []
    return feats if isinstance(feats, list) else []


def feature_value(row: dict, category: str) -> str | None:
    """Extract one feature category value from the row features list."""
    prefix = f"{category}: "
    for item in safe_features(row):
        if isinstance(item, str) and item.startswith(prefix):
            return item[len(prefix):].strip()
    return None


def feature_contains(row: dict, category: str, value: str) -> bool:
    """Case-insensitive substring match against a feature category."""
    needle = value.strip().lower()
    for item in safe_features(row):
        if not isinstance(item, str) or not item.startswith(f"{category}: "):
            continue
        haystack = item.split(": ", 1)[1].strip().lower()
        if needle in haystack:
            return True
    return False


def feature_rank_ok(row: dict, min_rank: int) -> bool:
    """Return whether the listing energy rank is at least as good as min_rank."""
    from config import energy_rank

    val = feature_value(row, "Energy")
    if val is None:
        return False
    rank = energy_rank(val)
    if rank is None:
        logger.warning("房源 %r 能耗标签不在白名单中: %r", row.get("id"), val)
        return False
    return rank <= min_rank


def row_to_listing(row: dict) -> Listing:
    """Convert a SQLite listing row dict into models.Listing for filters."""
    return Listing(
        id=row.get("id", "") or "",
        name=row.get("name", "") or "",
        status=row.get("status", "") or "",
        price_raw=row.get("price_raw") or None,
        available_from=row.get("available_from") or None,
        features=safe_features(row),
        url=row.get("url", "") or "",
        city=row.get("city", "") or "",
    )


def apply_user_filter(
    rows: Iterable[dict],
    user: Optional[UserConfig],
) -> list[dict]:
    """
    Apply a user's ListingFilter to listing rows.

    user is None    -> admin/guest view, pass through
    empty filter    -> pass through
    configured user -> ListingFilter.passes(row_to_listing(row))
    """
    rows_list = list(rows)
    if user is None:
        return rows_list
    listing_filter = user.listing_filter
    if listing_filter.is_empty():
        return rows_list

    out: list[dict] = []
    for row in rows_list:
        try:
            if listing_filter.passes(row_to_listing(row)):
                out.append(row)
        except Exception:
            logger.exception("apply_user_filter: 过滤异常 id=%s", row.get("id"))
    return out


def serialize_listing(row: dict) -> dict:
    """Stable API v1 listing JSON shape."""
    from models import parse_features_list, parse_float

    feats = safe_features(row)
    feature_map = parse_features_list(feats)
    return {
        "id": row.get("id", ""),
        "name": row.get("name", ""),
        "status": row.get("status", ""),
        "price_raw": row.get("price_raw") or "",
        "price_value": parse_float(row.get("price_raw", "")),
        "available_from": row.get("available_from") or "",
        "city": row.get("city") or "",
        "url": row.get("url") or "",
        "features": feats,
        "feature_map": feature_map,
        "first_seen": row.get("first_seen") or "",
        "last_seen": row.get("last_seen") or "",
    }


def serialize_filter(user: Optional[UserConfig]) -> dict:
    """Serialize a user's listing filter; admin/guest returns empty filter."""
    if user is None:
        return {}
    return asdict(user.listing_filter)


def query_listing_rows(
    *,
    user: UserConfig | None = None,
    status: str | None = None,
    search: str | None = None,
    cities: list[str] | None = None,
    types: list[str] | None = None,
    contract: str | None = None,
    energy: str | None = None,
    max_rent: float | None = None,
    min_area: float | None = None,
    tenants: list[str] | None = None,
    finishing: str | None = None,
    limit: int = 2000,
) -> list[dict]:
    """
    Query listings and apply shared Python-side filters.

    Single-city filters are pushed into SQL; multi-city filters are applied in
    Python to preserve the existing route behavior.
    """
    from config import energy_rank
    from models import parse_features_list, parse_float

    cities = cities or []
    types = types or []
    tenants = tenants or []
    sql_city = cities[0] if len(cities) == 1 else None

    with storage_ctx() as st:
        rows = st.get_all_listings(
            status=status,
            search=search,
            city=sql_city,
            limit=limit,
        )

    rows = apply_user_filter(rows, user)

    if len(cities) > 1:
        city_set = {c.lower() for c in cities}
        rows = [r for r in rows if (r.get("city") or "").lower() in city_set]
    if max_rent is not None:
        rows = [
            r for r in rows
            if (price := parse_float(r.get("price_raw", ""))) is not None
            and price <= max_rent
        ]
    if min_area is not None:
        def _area(row: dict) -> float | None:
            feature_map = parse_features_list(safe_features(row))
            return parse_float(feature_map.get("area", ""))

        rows = [r for r in rows if (area := _area(r)) is not None and area >= min_area]
    if types:
        rows = [
            r for r in rows
            if any(feature_contains(r, "Type", t) for t in types)
        ]
    if contract:
        rows = [r for r in rows if feature_contains(r, "Contract", contract)]
    if tenants:
        rows = [
            r for r in rows
            if any(feature_contains(r, "Tenant", tenant) for tenant in tenants)
        ]
    if energy:
        min_rank = energy_rank(energy)
        if min_rank is not None:
            rows = [r for r in rows if feature_rank_ok(r, min_rank)]
        else:
            logger.warning("无效能耗筛选参数 %r，已忽略", energy)
    if finishing:
        rows = [r for r in rows if feature_contains(r, "Finishing", finishing)]

    return rows


def get_listing_detail(listing_id: str, user: UserConfig | None = None) -> dict | None:
    """Return one listing row, respecting user listing_filter visibility."""
    with storage_ctx() as st:
        row = st.conn.execute(
            "SELECT * FROM listings WHERE id = ?",
            (listing_id,),
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    if user is not None and not user.listing_filter.is_empty():
        if not apply_user_filter([result], user):
            return None
    return result


def get_filter_options() -> dict[str, Any]:
    """Return Web listing filter option values."""
    with storage_ctx() as st:
        statuses = st.get_distinct_statuses()
        cities = st.get_distinct_cities()
        contracts = st.get_feature_values("Contract")
        tenants = st.get_feature_values("Tenant")
        from config import ENERGY_LABELS, energy_rank

        raw_energy = st.get_feature_values("Energy")
        energies = sorted(
            [x for x in raw_energy if x.upper() in ENERGY_LABELS] or ENERGY_LABELS,
            key=lambda e: energy_rank(e) if energy_rank(e) is not None else 99,
        )
        finishings = st.get_feature_values("Finishing")
    return {
        "statuses": statuses,
        "cities": cities,
        "contracts": contracts,
        "tenants": tenants,
        "energies": energies,
        "finishings": finishings,
    }


def _filter_prebuilt_rows_by_user(
    st: Any,
    rows: list[dict],
    user: UserConfig | None,
) -> list[dict]:
    """Filter map/calendar prebuilt rows by looking up raw listing rows."""
    if user is None or user.listing_filter.is_empty():
        return rows
    ids = [row["id"] for row in rows if row.get("id")]
    if not ids:
        return rows
    placeholders = ",".join("?" * len(ids))
    raw_rows = st.conn.execute(
        f"SELECT * FROM listings WHERE id IN ({placeholders})",
        ids,
    ).fetchall()
    kept = {row["id"] for row in apply_user_filter([dict(r) for r in raw_rows], user)}
    return [row for row in rows if row.get("id") in kept]


def get_map_payload(user: UserConfig | None = None) -> dict[str, Any]:
    """Return cached-coordinate map payload without triggering geocoding."""
    results: list[dict] = []
    uncached = 0
    with storage_ctx() as st:
        listings = _filter_prebuilt_rows_by_user(st, st.get_map_listings(), user)
        for listing in listings:
            cached = st.get_cached_coords(listing["address"])
            if cached:
                lat, lng = cached
                results.append({**listing, "lat": lat, "lng": lng})
            else:
                uncached += 1
    return {"listings": results, "uncached": uncached}


def get_calendar_payload(user: UserConfig | None = None) -> dict[str, Any]:
    """Return calendar payload, optionally filtered for a user."""
    with storage_ctx() as st:
        listings = _filter_prebuilt_rows_by_user(st, st.get_calendar_listings(), user)
    return {"listings": listings}

