"""地图坐标缓存 + 日历视图查询。"""

from __future__ import annotations

import json
import logging

from models import parse_features_list

logger = logging.getLogger(__name__)

# 荷兰城市口语别称 → 正式名
_CITY_FORMAL: dict[str, str] = {
    "Den Bosch": "'s-Hertogenbosch",
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

    def get_map_listings(self) -> list[dict]:
        rows = self._conn.execute(
            """SELECT id, name, status, price_raw, available_from, url, city, source, features
               FROM listings ORDER BY city, name LIMIT 2000"""
        ).fetchall()
        results: list[dict] = []
        for r in rows:
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
            results.append({
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
            })
        return results
