"""10 个统计图表查询 + 2 个共享 helper。"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from models import parse_float, parse_int

logger = logging.getLogger(__name__)


class ChartOps:
    """依赖 self._conn / self._tz。"""

    # ── 趋势（折线图）────────────────────────────────────────────────

    def chart_daily_new(self, days: int = 30) -> list[dict]:
        tz = ZoneInfo(self._tz)
        now_local = datetime.now(tz)
        today_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        start_local = today_local - timedelta(days=days)
        cutoff_utc = (start_local - timedelta(days=1)).isoformat()

        rows = self._conn.execute(
            "SELECT first_seen FROM listings WHERE first_seen >= ?", (cutoff_utc,)
        ).fetchall()

        day_counts: dict[str, int] = {}
        for (ts,) in rows:
            utc_dt = datetime.fromisoformat(ts)
            local_date = utc_dt.astimezone(tz).strftime("%Y-%m-%d")
            day_counts[local_date] = day_counts.get(local_date, 0) + 1

        result: list[dict] = []
        for i in range(days, -1, -1):
            d = (today_local - timedelta(days=i)).strftime("%Y-%m-%d")
            result.append({"date": d, "count": day_counts.get(d, 0)})
        return result

    def chart_daily_changes(self, days: int = 30) -> list[dict]:
        tz = ZoneInfo(self._tz)
        now_local = datetime.now(tz)
        today_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        start_local = today_local - timedelta(days=days)
        cutoff_utc = (start_local - timedelta(days=1)).isoformat()

        rows = self._conn.execute(
            "SELECT changed_at FROM status_changes WHERE changed_at >= ?", (cutoff_utc,)
        ).fetchall()

        day_counts: dict[str, int] = {}
        for (ts,) in rows:
            utc_dt = datetime.fromisoformat(ts)
            local_date = utc_dt.astimezone(tz).strftime("%Y-%m-%d")
            day_counts[local_date] = day_counts.get(local_date, 0) + 1

        result: list[dict] = []
        for i in range(days, -1, -1):
            d = (today_local - timedelta(days=i)).strftime("%Y-%m-%d")
            result.append({"date": d, "count": day_counts.get(d, 0)})
        return result

    # ── 分布（饼图/柱状图）───────────────────────────────────────────

    def chart_city_dist(self) -> list[dict]:
        rows = self._conn.execute(
            """SELECT COALESCE(NULLIF(city,''), '未知') AS city, COUNT(*) AS cnt
               FROM listings GROUP BY city ORDER BY cnt DESC"""
        ).fetchall()
        return [{"city": r["city"], "count": r["cnt"]} for r in rows]

    def chart_status_dist(self) -> list[dict]:
        rows = self._conn.execute(
            """SELECT COALESCE(NULLIF(status,''), '未知') AS status, COUNT(*) AS cnt
               FROM listings GROUP BY status ORDER BY cnt DESC"""
        ).fetchall()
        return [{"status": r["status"], "count": r["cnt"]} for r in rows]

    def chart_price_dist(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT price_raw FROM listings WHERE price_raw IS NOT NULL AND price_raw != ''"
        ).fetchall()

        buckets: dict[str, int] = {
            "<€600": 0, "€600-700": 0, "€700-800": 0, "€800-900": 0,
            "€900-1000": 0, "€1000-1200": 0, "€1200-1400": 0,
            "€1400-1600": 0, ">€1600": 0,
        }
        for (raw,) in rows:
            price = parse_float(raw)
            if price is None:
                continue
            if price < 600:       buckets["<€600"] += 1
            elif price < 700:     buckets["€600-700"] += 1
            elif price < 800:     buckets["€700-800"] += 1
            elif price < 900:     buckets["€800-900"] += 1
            elif price < 1000:    buckets["€900-1000"] += 1
            elif price < 1200:    buckets["€1000-1200"] += 1
            elif price < 1400:    buckets["€1200-1400"] += 1
            elif price < 1600:    buckets["€1400-1600"] += 1
            else:                 buckets[">€1600"] += 1

        return [{"range": k, "count": v} for k, v in buckets.items()]

    def chart_hourly_dist(self) -> list[dict]:
        tz = ZoneInfo(self._tz)
        rows = self._conn.execute(
            "SELECT first_seen FROM listings WHERE first_seen IS NOT NULL"
        ).fetchall()

        counts: dict[int, int] = {h: 0 for h in range(24)}
        for (ts,) in rows:
            utc_dt = datetime.fromisoformat(ts)
            local_hour = utc_dt.astimezone(tz).hour
            counts[local_hour] = counts.get(local_hour, 0) + 1

        return [{"hour": h, "count": counts[h]} for h in range(24)]

    # ── 共享 helper ─────────────────────────────────────────────────

    def _count_feature_values(self, category: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT features FROM listings WHERE features IS NOT NULL AND features != '[]'"
        ).fetchall()
        counts: dict[str, int] = {}
        prefix = f"{category}: "
        for (features_json,) in rows:
            try:
                feats = json.loads(features_json)
            except (json.JSONDecodeError, TypeError):
                logger.warning("图表统计: 跳过损坏的 features JSON: %.80s", features_json)
                continue
            for f in feats:
                if f.startswith(prefix):
                    val = f[len(prefix):].strip()
                    if val:
                        counts[val] = counts.get(val, 0) + 1
                    break
        return [{"label": k, "count": v}
                for k, v in sorted(counts.items(), key=lambda x: -x[1])]

    def _bucketed_number_dist(
        self, category: str, buckets: dict[str, int], classifier
    ) -> list[dict]:
        rows = self._conn.execute(
            "SELECT features FROM listings WHERE features IS NOT NULL AND features != '[]'"
        ).fetchall()
        prefix = f"{category}: "
        for (features_json,) in rows:
            try:
                feats = json.loads(features_json)
            except (json.JSONDecodeError, TypeError):
                logger.warning("图表统计: 跳过损坏的 features JSON: %.80s", features_json)
                continue
            for f in feats:
                if f.startswith(prefix):
                    val = f[len(prefix):].strip()
                    if val:
                        classifier(val, buckets)
                    break
        return [{"label": k, "count": v} for k, v in buckets.items()]

    # ── 具体分布图表 ────────────────────────────────────────────────

    def chart_tenant_dist(self) -> list[dict]:
        return self._count_feature_values("Tenant")

    def chart_contract_dist(self) -> list[dict]:
        return self._count_feature_values("Contract")

    def chart_type_dist(self) -> list[dict]:
        data = self._count_feature_values("Type")
        def _rank(label: str) -> tuple:
            lower = label.lower().strip()
            if "studio" in lower:  return (0, 0)
            if "loft" in lower:    return (0, 1)
            try:                   return (1, int(lower))
            except ValueError:     pass
            return (2, 0)
        data.sort(key=lambda r: _rank(r["label"]))
        return data

    def chart_energy_dist(self) -> list[dict]:
        data = self._count_feature_values("Energy")
        def _rank(label: str) -> tuple:
            upper = label.upper().strip()
            if not upper: return (999, 0)
            pluses = upper.count("+")
            base = upper.replace("+", "").strip()
            letter_order = ord(base[0]) if base and base[0].isalpha() else 999
            return (letter_order, -pluses)
        data.sort(key=lambda r: _rank(r["label"]))
        return data

    def chart_area_dist(self) -> list[dict]:
        buckets = {"<20 m²": 0, "20-30 m²": 0, "30-50 m²": 0, "50-80 m²": 0, ">80 m²": 0}
        def _classify(val: str, b: dict):
            area = parse_float(val)
            if area is None: return
            if area < 20:   b["<20 m²"] += 1
            elif area < 30: b["20-30 m²"] += 1
            elif area < 50: b["30-50 m²"] += 1
            elif area < 80: b["50-80 m²"] += 1
            else:           b[">80 m²"] += 1
        return self._bucketed_number_dist("Area", buckets, _classify)

    def chart_floor_dist(self) -> list[dict]:
        buckets = {"Ground": 0, "1-2": 0, "3-5": 0, "6+": 0}
        def _classify(val: str, b: dict):
            floor = parse_int(val)
            if floor is None: return
            if floor == 0:   b["Ground"] += 1
            elif floor <= 2: b["1-2"] += 1
            elif floor <= 5: b["3-5"] += 1
            else:            b["6+"] += 1
        return self._bucketed_number_dist("Floor", buckets, _classify)
