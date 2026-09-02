"""
Shared listing read/query service.

This module keeps data access, feature parsing, and user listing_filter
application out of route handlers. Web routes and API v1 routes can keep their
own auth and response envelopes while sharing the same listing behavior.
"""
from __future__ import annotations

import math

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


def dim_applies(row: dict, dim: str) -> bool:
    """该房源所属平台是否提供这个维度。不提供时条件对它整体跳过。

    与 ``ListingFilter.passes``（通知那条路）保持同一语义。此前浏览页没有这一层
    ——一条没有 Finishing 字段的房源在这里必然不匹配，于是勾「装修 = Furnished」
    会把 Xior 与 OurDomain 的 83 条整个排除，而通知侧放行。同一个条件两个答案。

    放行是对的：这两家的房源实际都是带家具的，只是 feed 里不上报该属性；按
    「没这个字段就不匹配」处理等于因为上游少给一个字段而把整个平台从结果里
    抹掉。
    """
    from config import source_supports_dim

    return source_supports_dim(row.get("source") or "holland2stay", dim)


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
    # 以下维度并非每个平台都提供。平台不支持 → 该条件对它整体跳过（fail-open，
    # 见 dim_applies）；平台支持但本条缺值 → 照常不匹配，不削弱严格度。
    if types:
        rows = [
            r for r in rows
            if not dim_applies(r, "type")
            or any(feature_contains(r, "Type", t) for t in types)
        ]
    if contract:
        rows = [
            r for r in rows
            if not dim_applies(r, "contract")
            or feature_contains(r, "Contract", contract)
        ]
    if tenants:
        rows = [
            r for r in rows
            if not dim_applies(r, "tenant")
            or any(feature_contains(r, "Tenant", tenant) for tenant in tenants)
        ]
    if occupancies:
        # Occupancy feature 值形如 "Single" / "Two (only couples)" / "Three" 等。
        # 多选语义：OR，命中任意一个值即通过。
        rows = [
            r for r in rows
            if not dim_applies(r, "occupancy")
            or any(feature_contains(r, "Occupancy", occ) for occ in occupancies)
        ]
    if energy:
        min_rank = energy_rank(energy)
        if min_rank is not None:
            rows = [
                r for r in rows
                if not dim_applies(r, "energy") or feature_rank_ok(r, min_rank)
            ]
        else:
            logger.warning("无效能耗筛选参数 %r，已忽略", energy)
    if finishing:
        # 装修是四档互斥，多选语义为 OR：命中任意一档即通过。
        # 兼容传单个字符串——API 与旧链接都可能只带一个值。
        wanted = [finishing] if isinstance(finishing, str) else list(finishing)
        rows = [
            r for r in rows
            if not dim_applies(r, "finishing")
            or any(feature_contains(r, "Finishing", f) for f in wanted)
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
    return {"listings": spread_stacked_coords(results), "uncached": uncached}


#: 同址散开的基准半径（纬度度数）。0.00012° ≈ 13 m。
#:
#: 一栋楼的每个单元共用同一个街道地址，geocode 出来是**同一个坐标**。生产实测
#: 235 条里有 66 条（28%）压在别人身上，最多的一处十套叠一个点。这种点在 Web 上
#: 低于 zoom 17 是个永远点不开的聚合泡，高过 zoom 17 则是十个圆点画在同一个像素
#: 上；在 iOS 上更糟——网格聚类对**完全重合**的点在任何 cell 大小下都归一格，点
#: 击展开又会被 boundingRegion 的 minSpan 兜成固定视野，于是那十套一套都碰不到。
#:
#: 摆成一圈解决。半径随数量增长，让相邻两点的弧长大致恒定，十套和三套看起来一样
#: 疏；排序用 id，同一套房每次刷新都落在圈上的同一个位置，不会跳。
#:
#: **放在服务端而不是各端各写一遍**：几何本身只有十几行，但 Web/iOS/Android 三份
#: 实现迟早分叉，而分叉的表现是同一套房在不同端显示在不同位置——没有任何地方会
#: 报错。客户端只需要认 display_lat / display_lng / stack_n 三个字段。
MAP_SPREAD_BASE_DEG = 0.00012


def spread_stacked_coords(rows: list[dict]) -> list[dict]:
    """就地写入 ``display_lat`` / ``display_lng`` / ``stack_n``，返回同一个列表。

    位置因此是**近似值**，客户端在 ``stack_n > 1`` 时必须说明这一点——不说的话
    用户会以为图钉就是门牌号。
    """
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        try:
            lat, lng = float(row["lat"]), float(row["lng"])
        except (KeyError, TypeError, ValueError):
            continue
        groups.setdefault((f"{lat:.6f}", f"{lng:.6f}"), []).append(row)

    for group in groups.values():
        n = len(group)
        for row in group:
            row["stack_n"] = n
        if n == 1:
            group[0]["display_lat"] = float(group[0]["lat"])
            group[0]["display_lng"] = float(group[0]["lng"])
            continue
        group.sort(key=lambda r: str(r.get("id", "")))
        lat0, lng0 = float(group[0]["lat"]), float(group[0]["lng"])
        radius = MAP_SPREAD_BASE_DEG * max(1.0, n / 6.0)
        # 经度一度的实际距离随纬度收缩；不补这一下，圈在图上会被压成椭圆。
        lng_scale = 1.0 / max(0.2, math.cos(math.radians(lat0)))
        for i, row in enumerate(group):
            angle = 2 * math.pi * i / n - math.pi / 2
            row["display_lat"] = lat0 + radius * math.sin(angle)
            row["display_lng"] = lng0 + radius * math.cos(angle) * lng_scale
    return rows


def locate_map_listing(listing_id: str) -> dict[str, Any]:
    """按 id 定位单条房源，**绕过新鲜度窗口与用户筛选**。

    ``/map?focus=<id>`` 用它兜底。前端只在「这个 id 不在已渲染的集合里」时才
    调用，所以这里的任务不是再判一次可见性，而是把「为什么看不到」分成两种
    可以分别应对的答案：

    - ``not_found``：库里根本没有这个 id（链接过期 / 手改的 URL）
    - ``no_coords``：房源在，但地址还没解析出坐标——地图上确实没有这个点
    - 成功：房源在、坐标也在，只是被 14 天窗口或用户筛选挡在视图之外；
      前端据此单独落一个标记并说明情况

    三者绝不能合并成一句「没找到」。合并之后，「这套下架了」和「你的筛选把它
    藏了」在界面上长得一模一样，而用户能做的事完全不同。
    """
    with storage_ctx() as st:
        entry = st.get_map_listing_by_id(listing_id)
        if entry is None:
            return {"ok": False, "reason": "not_found"}
        cached = st.get_cached_coords(entry["address"])
        if not cached:
            return {"ok": False, "reason": "no_coords", "listing": entry}
        lat, lng = cached
        # 这一条是视图之外单独落的一枚标记，同址的其余几套并不在图上，
        # 没有可散开的对象——给真实坐标，并显式带上 stack_n=1，免得客户端
        # 因为字段缺失去猜。
        return {"ok": True, "listing": {
            **entry, "lat": lat, "lng": lng,
            "display_lat": lat, "display_lng": lng, "stack_n": 1,
        }}


def get_calendar_payload(user: UserConfig | None = None) -> dict[str, Any]:
    """Return calendar payload, optionally filtered for a user."""
    with storage_ctx() as st:
        listings = _filter_prebuilt_rows_by_user(st, st.get_calendar_listings(), user)
    return {"listings": listings}
