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
    """Yield a storage instance.

    Flask request context → g._storage (auto-closed by teardown_appcontext).
    Outside request context → new instance (caller must close via context manager).
    """
    from flask import has_request_context

    st = storage()
    try:
        yield st
    finally:
        if not has_request_context():
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


#: 浏览页的 feature 类目 → ListingFilter 的维度名。
#:
#: 两边的叫法不一致（页面上是 ``Finishing``，能力表里是 ``finishing``），而匹配
#: 方式要按维度查表决定，所以必须能对上。
_FEATURE_CATEGORY_DIMS = {
    "Finishing": "finishing",
    "Type": "type",
    "Occupancy": "occupancy",
    "Contract": "contract",
    "Tenant": "tenant",
    "Offer": "offer",
    "Neighborhood": "neighborhood",
}

#: 取值受控、可以安全归一的类目。
#:
#: Neighborhood 刻意不在其中：它是自由文本（片区名），而同义表按整个值查，
#: 扫过自由文本会误伤——某个片区若恰好叫 ``Kaal``，会被改写成 ``Unfurnished``。
_NORMALIZED_CATEGORIES = frozenset({
    "Finishing", "Type", "Occupancy", "Contract", "Tenant", "Offer",
})


def feature_contains(row: dict, category: str, value: str) -> bool:
    """房源的某个 feature 类目是否命中筛选值。

    判据与 ``ListingFilter.passes``（通知那条路）**共用** ``whitelist_matches``。

    这里原先是另写的一套裸子串匹配。两套实现意味着同一个筛选条件在「浏览页」
    和「通知」上给出不同结果——2026-08-05 修好通知侧的荷兰语归一与装修分档
    之后，浏览页仍然按老规矩走：勾「装修 = Furnished」页面回 251 条，其中含
    Semi / Fully / Unfurnished，而通知只发 187 条真正的 Furnished。
    """
    from config import whitelist_matches

    dim = _FEATURE_CATEGORY_DIMS.get(category, "")
    for item in safe_features(row):
        if not isinstance(item, str) or not item.startswith(f"{category}: "):
            continue
        if whitelist_matches(value, item.split(": ", 1)[1], dim):
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
    out["features"] = _canonical_features_json(out)
    return out


def _canonical_features_json(row: dict) -> str:
    """把 features 里的取值归一到规范写法，返回重新序列化的 JSON。

    上游同一属性有荷兰语和英语两版。筛选侧已经归一，展示侧不归一就会对不上：
    下拉里写 ``Two (only couples)``，房源卡片上却是 ``Twee (alleen koppels)``，
    用户会怀疑筛选是不是把这条漏了。

    **只处理受控取值的类目**（``_NORMALIZED_CATEGORIES``）。同义表是按整个值
    查的，扫过自由文本会误伤：片区或楼盘若恰好叫 ``Kaal``，会被改写成
    ``Unfurnished``。类目名不动，原始值仍在数据库里，这里只影响展示。
    """
    from models import canonical_feature

    feats = safe_features(row)
    if not feats:
        return row.get("features", "[]") or "[]"
    out = []
    for item in feats:
        if isinstance(item, str) and ": " in item:
            cat, val = item.split(": ", 1)
            if cat in _NORMALIZED_CATEGORIES:
                item = f"{cat}: {canonical_feature(val)}"
        out.append(item)
    return json.dumps(out, ensure_ascii=False)


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
        # status 是系统推测的，还是平台自己说的。
        #
        # 平台不会告诉我们「这个单元没了」——它只是把那一行从列表里拿掉。所以
        # ``mark_stale_listings`` 在房源老化后把它标成 Occupied 并置这个位。
        # 客户端**必须能区分**：一个「平台报的 Occupied」和一个「我们猜的
        # Occupied」可信度差得远，混在一起等于把推断当事实端给用户。
        "status_is_inferred": bool(row.get("status_is_inferred") or 0),
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
    finishing: str | list[str] | None = None,
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
        # 和单选那条路（SQL 里的 city_normalized）保持同一套判据。
        # 用原始 city 比会让多选和单选给出不同结果——单选「Utrecht」命中 Xior
        # 的楼盘，多选「Utrecht + Rotterdam」反而漏掉。
        from config import canonical_city

        city_set = {canonical_city(c).lower() for c in cities}
        rows = [
            r for r in rows
            if (r.get("city_normalized") or canonical_city(r.get("city") or "")).lower()
            in city_set
        ]
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
        # 装修是四档互斥，多选语义为 OR：命中任意一档即通过。
        # 兼容传单个字符串——API 与旧链接都可能只带一个值。
        wanted = [finishing] if isinstance(finishing, str) else list(finishing)
        rows = [
            r for r in rows
            if any(feature_contains(r, "Finishing", f) for f in wanted)
        ]

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
