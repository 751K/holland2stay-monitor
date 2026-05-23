"""
Shared listing read/query service.

This module keeps data access, feature parsing, and user listing_filter
application out of route handlers. Web routes and API v1 routes can keep their
own auth and response envelopes while sharing the same listing behavior.
"""
from __future__ import annotations

import json
import logging
import re
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


def normalize_listing_row(row: dict) -> dict:
    """Return a display-normalized copy of one listing row."""
    out = dict(row)
    source = (out.get("source") or "holland2stay").lower()
    if source == "ourdomain":
        out["name"] = _ourdomain_display_name(out)
    elif source == "xior":
        out["name"] = _xior_display_name(out)
    return out


def normalize_listing_rows(rows: Iterable[dict]) -> list[dict]:
    """Normalize rows for Web/API display without mutating storage results."""
    return [normalize_listing_row(r) for r in rows]


def _ourdomain_display_name(row: dict) -> str:
    unit = feature_value(row, "Unit") or _extract_ourdomain_unit(row.get("name", ""))
    if not unit:
        return str(row.get("name") or "")
    building = feature_value(row, "Building") or row.get("city") or "Diemen"
    building = _short_ourdomain_building(str(building))
    unit = unit.strip()
    if not unit.startswith("#"):
        unit = f"#{unit}"
    return f"{building} {unit}".strip()


def _extract_ourdomain_unit(name: str) -> str:
    m = re.search(r"#?\b(\d{3,})\b", name or "")
    return f"#{m.group(1)}" if m else ""


def _short_ourdomain_building(building: str) -> str:
    value = building.strip()
    lower = value.lower()
    if lower == "amsterdam diemen" or lower.endswith(" diemen"):
        return "Diemen"
    if "south-east" in lower or "south east" in lower:
        return "South East"
    return value or "Diemen"


def _xior_display_name(row: dict) -> str:
    """Xior listing display: 'Maastricht Annadal M1.30.53' → 'M1.30.53'"""
    unit = feature_value(row, "Unit") or ""
    building = feature_value(row, "Building") or ""
    if unit:
        return unit
    # fallback: extract from raw name
    name = row.get("name", "")
    if " " in name:
        parts = name.split(" ", 2)
        if len(parts) >= 3:
            return parts[-1]
    return name


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
    row = normalize_listing_row(row)
    return Listing(
        id=row.get("id", "") or "",
        name=row.get("name", "") or "",
        status=row.get("status", "") or "",
        price_raw=row.get("price_raw") or None,
        available_from=row.get("available_from") or None,
        features=safe_features(row),
        url=row.get("url", "") or "",
        city=row.get("city", "") or "",
        source=row.get("source") or "holland2stay",
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

    row = normalize_listing_row(row)
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
        "source": row.get("source") or "holland2stay",
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
    sources: list[str] | None = None,
    types: list[str] | None = None,
    contract: str | None = None,
    energy: str | None = None,
    max_rent: float | None = None,
    min_area: float | None = None,
    tenants: list[str] | None = None,
    occupancies: list[str] | None = None,
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
    sources = sources or []
    types = types or []
    tenants = tenants or []
    occupancies = occupancies or []
    sql_city = cities[0] if len(cities) == 1 else None
    sql_source = sources[0] if len(sources) == 1 else None

    with storage_ctx() as st:
        rows = st.get_all_listings(
            status=status,
            search=search,
            city=sql_city,
            source=sql_source,
            limit=limit,
        )

    rows = apply_user_filter(rows, user)

    if len(cities) > 1:
        city_set = {c.lower() for c in cities}
        rows = [r for r in rows if (r.get("city") or "").lower() in city_set]
    if len(sources) > 1:
        source_set = {s.lower() for s in sources}
        rows = [r for r in rows if (r.get("source") or "holland2stay").lower() in source_set]
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
    if occupancies:
        # Occupancy feature 值形如 "Single" / "Two (only couples)" / "Three" 等。
        # 多选语义：OR，命中任意一个值即通过。
        rows = [
            r for r in rows
            if any(feature_contains(r, "Occupancy", occ) for occ in occupancies)
        ]
    if energy:
        min_rank = energy_rank(energy)
        if min_rank is not None:
            rows = [r for r in rows if feature_rank_ok(r, min_rank)]
        else:
            logger.warning("无效能耗筛选参数 %r，已忽略", energy)
    if finishing:
        rows = [r for r in rows if feature_contains(r, "Finishing", finishing)]

    return normalize_listing_rows(rows)


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
    return normalize_listing_row(result)


def get_filter_options() -> dict[str, Any]:
    """Return Web listing filter option values."""
    with storage_ctx() as st:
        statuses = st.get_distinct_statuses()
        cities = st.get_distinct_cities()
        sources = st.get_distinct_sources()
        # 新增维度：Type（房型）+ Occupancy（允许入住人数）。
        # 都是从 listings.features 里 distinct 提取的——values 取决于已抓取的源
        # （H2S Studio / 1 / Loft；OurDomain Studio / 1-Bedroom Apartment / 1-Bedroom Loft）。
        types = st.get_feature_values("Type")
        occupancies = st.get_feature_values("Occupancy")
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
        "sources": sources,
        "types": types,
        "occupancies": occupancies,
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
