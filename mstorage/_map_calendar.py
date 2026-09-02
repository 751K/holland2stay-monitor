"""地图坐标缓存 + 日历视图查询。"""

from __future__ import annotations

import json
import logging
import os

from models import parse_features_list

logger = logging.getLogger(__name__)

#: 地图只显示这么多天内还被抓到过的房源。
#:
#: 为什么需要这道过滤：``Occupied`` 是老化收敛的**终态**，那些行永远留在库里。
#: 地图此前不带任何时间条件，于是几个月前就从 feed 里消失的单元仍然钉在图上。
#: 2026-08-28 线上实测 628 条里有 270 条超过 30 天没被抓到过，全部是 Occupied，
#: 而 Reserved 与可订的房源没有一条陈旧——真正的噪音全在终态那一侧。
#:
#: 十四天：在 feed 里的房源每轮都会被看到（轮次是分钟级），因此「两周没见到」
#: 已经足够肯定它不在了；同时留下两周的近期成交做参考，不至于把地图清空。
#: 用 MAP_MAX_AGE_DAYS 可调，设 0 表示不过滤。
#:
#: 比较必须走 julianday 而不是字符串。``last_seen`` 存的是带时区的 ISO
#: （``2026-08-14T09:00:00+00:00``），而 ``datetime('now','-14 days')`` 返回的是
#: 空格分隔、无时区的形式；两者直接比字符串时，第 10 位是 ``T``(0x54) 对空格
#: (0x20)，于是边界那一天的房源无论几点都判为「新」。差一天看不出来，但它是
#: 那种永远不会报错的错。
_MAP_MAX_AGE_DAYS_DEFAULT = 14


def _map_max_age_days() -> int:
    raw = os.environ.get("MAP_MAX_AGE_DAYS", "")
    if not raw.strip():
        return _MAP_MAX_AGE_DAYS_DEFAULT
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        logger.warning("MAP_MAX_AGE_DAYS=%r 不是整数，按默认 %d 天处理",
                       raw, _MAP_MAX_AGE_DAYS_DEFAULT)
        return _MAP_MAX_AGE_DAYS_DEFAULT

# 荷兰城市口语别称 → 正式名
_CITY_FORMAL: dict[str, str] = {
    "Den Bosch": "'s-Hertogenbosch",
}


#: 地图条目用到的列。两处查询（全量 / 按 id）共用，避免改一处漏一处。
_MAP_COLUMNS = "id, name, status, price_raw, available_from, url, city, source, features"


def row_to_map_entry(r) -> dict:
    """把一行 listings 转成地图条目。

    ``address`` 的推导只有这一份：``get_map_listings`` 与
    ``get_map_listing_by_id`` 都走它。地址是 geocode 缓存的**主键**，两处若推
    出不同的字符串，深链就会查不到那套房已经缓存好的坐标——而且不报错。
    """
    try:
        feats = json.loads(r["features"] or "[]")
    except (json.JSONDecodeError, TypeError):
        feats = []
    feat_map = parse_features_list(feats)
    city = r["city"] or ""
    city_full = _CITY_FORMAL.get(city, city)
    # 优先用 features 里的 Address:（OurDomain 写入建筑街道地址，
    # 因为 unit 名是 "Diemen #6045" 这种内部编号，geocode 不到）。
    # H2S 不写 Address feature，回退到 name+city 老路径（name 本身
    # 含街道地址，例如 "Kastanjelaan 1-718, Eindhoven"）。
    street = feat_map.get("address", "").strip()
    if street:
        address = ", ".join(filter(None, [street, "Netherlands"]))
    else:
        address = ", ".join(filter(None, [r["name"], city_full, "Netherlands"]))
    return {
        "id": r["id"],
        "name": r["name"],
        "status": r["status"],
        "price_raw": r["price_raw"] or "",
        "available_from": r["available_from"] or "",
        "url": r["url"] or "",
        "city": r["city"] or "",
        "source": r["source"] or "holland2stay",
        "neighborhood": feat_map.get("neighborhood", ""),
        "building": feat_map.get("building", ""),
        "area": feat_map.get("area", ""),
        "address": address,
    }


class MapCalendarOps:
    """依赖 self._conn。"""

    # ── 日历 ────────────────────────────────────────────────────────

    def get_calendar_listings(self) -> list[dict]:
        rows = self._conn.execute(
            """SELECT id, name, status, price_raw, available_from, url, city, source, features
               FROM listings
               WHERE available_from IS NOT NULL AND available_from != ''
               ORDER BY available_from"""
        ).fetchall()
        results: list[dict] = []
        for r in rows:
            building = ""
            try:
                feats = json.loads(r["features"] or "[]")
            except (json.JSONDecodeError, TypeError):
                feats = []
            for f in feats:
                if f.startswith("Building: "):
                    building = f.split(": ", 1)[1]
                    break
            results.append({
                "id": r["id"],
                "name": r["name"],
                "status": r["status"],
                "price_raw": r["price_raw"],
                "available_from": r["available_from"],
                "url": r["url"],
                "city": r["city"] or "",
                "source": r["source"] or "holland2stay",
                "building": building,
            })
        return results

    # ── 地图 ────────────────────────────────────────────────────────

    def get_cached_coords(self, address: str) -> tuple[float, float] | None:
        row = self._conn.execute(
            "SELECT lat, lng FROM geocode_cache WHERE address = ?", (address,)
        ).fetchone()
        return (row["lat"], row["lng"]) if row else None

    def cache_coords(self, address: str, lat: float, lng: float) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO geocode_cache (address, lat, lng) VALUES (?, ?, ?)",
                (address, lat, lng),
            )

    def get_map_listings(self, *, max_age_days: int | None = None) -> list[dict]:
        """地图数据。默认只返回近期还被抓到过的房源，见 _MAP_MAX_AGE_DAYS_DEFAULT。

        ``max_age_days=0`` 关闭过滤（返回全部）；不传则读环境变量。
        """
        days = _map_max_age_days() if max_age_days is None else max(0, int(max_age_days))
        if days:
            rows = self._conn.execute(
                f"""SELECT {_MAP_COLUMNS}
                   FROM listings
                   WHERE julianday(last_seen) >= julianday('now') - ?
                   ORDER BY city, name LIMIT 2000""",
                (days,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                f"""SELECT {_MAP_COLUMNS}
                   FROM listings ORDER BY city, name LIMIT 2000"""
            ).fetchall()
        return [row_to_map_entry(r) for r in rows]

    def get_map_listing_by_id(self, listing_id: str) -> dict | None:
        """按 id 取单条地图条目，**不带新鲜度过滤**。

        深链（``/map?focus=<id>``）要能定位到已经下架的房源——用户是从房源列表
        点过来的，那一套确实存在，只是过了 14 天窗口。这里返回 None 只意味着
        「库里没有这个 id」，与「有但没坐标」是两件事，由调用方分别处置。
        """
        row = self._conn.execute(
            f"SELECT {_MAP_COLUMNS} FROM listings WHERE id = ?", (listing_id,)
        ).fetchone()
        return row_to_map_entry(row) if row else None
