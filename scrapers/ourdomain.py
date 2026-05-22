"""
scrapers/ourdomain.py — OurDomain / RENTCafe unit-level scraper
================================================================

OurDomain exposes availability through RENTCafe HTML endpoints rather than a
JSON API. The scraper uses a two-step flow:

1. ``floorplans.aspx`` discovers floor plan IDs.
2. ``rcLoadContent.ashx?contentclass=availableunits`` returns concrete units.

Floor-plan level availability is deliberately ignored because it can disagree
with unit-level rows. Listings are keyed by physical unit ID and prefixed with
``od_`` so they cannot collide with Holland2Stay URL slugs.
"""
from __future__ import annotations

from datetime import date
from html import unescape
import logging
import os
import re
import time
from typing import Optional

import curl_cffi.requests as req

from config import get_impersonate, get_proxy_url
from models import Listing

from .base import (
    RATE_LIMIT_BACKOFF,
    AbstractScraper,
    BlockedError,
    RateLimitError,
    ScrapeNetworkError,
    ScrapeResult,
    ScrapeTask,
    is_cloudflare_body,
)


logger = logging.getLogger(__name__)

_DEFAULT_IMPERSONATES: tuple[str, ...] = (
    "chrome131",
    "chrome124",
    "safari17_0",
    "edge101",
)


class OurDomainScraper(AbstractScraper):
    """Unit-level scraper for OurDomain properties backed by RENTCafe."""

    source = "ourdomain"

    BASE = "https://thisisourdomain.securerc.co.uk/onlineleasing"

    # 每栋楼一份元数据：slug + property_id（用于 RentCafe URL）+ street_address
    # （用于 geocode）+ short_display（listing 名前缀）。
    #
    # street_address 必须是 OurDomain 该楼的**真实街道地址**——unit 名（如
    # "Diemen #6045"）是内部单元号，不可 geocode。每栋楼所有单元共享同一
    # 街道地址（地图上同一个 pin），符合"同栋楼"的物理事实。
    BUILDINGS: dict[str, dict[str, str]] = {
        "diemen": {
            "slug": "ourdomain-amsterdam-diemen",
            "display": "Amsterdam Diemen",
            "short_display": "Diemen",
            "property_id": "184283",
            "type": "Studio",
            "street_address": "Wenckebachweg 51, 1096 AN Amsterdam",
        },
        "south-east": {
            "base": "https://southeast-thisisourdomain.securerc.co.uk/onlineleasing",
            "slug": "ourdomain-amsterdam-south-east",
            "display": "Amsterdam South-East",
            "short_display": "South-East",
            "property_id": "182801",
            "type": "Studio",
            "street_address": "Dalsteindreef 20-40, 1112 XC Diemen",
        },
    }

    def scrape(self, task: ScrapeTask) -> ScrapeResult:
        building = self._building_for_task(task)
        slug = building["slug"]
        property_id = building["property_id"]
        base = building.get("base", self.BASE)
        display = task.city_display or building["display"]
        move_in_date = task.extra.get("move_in_date") or _next_month_first()

        proxy = get_proxy_url()
        proxies = {"https": proxy, "http": proxy} if proxy else {}

        floorplans_url = f"{base}/{slug}/floorplans.aspx"
        attempts = _impersonate_attempts()
        tried: list[str] = []
        last_blocked: BlockedError | None = None

        for idx, impersonate in enumerate(attempts, start=1):
            tried.append(impersonate)
            try:
                all_units, complete, fp_names_by_id = self._scrape_once(
                    display=display,
                    base=base,
                    floorplans_url=floorplans_url,
                    property_id=property_id,
                    move_in_date=move_in_date,
                    proxies=proxies,
                    impersonate=impersonate,
                )
                break
            except BlockedError as e:
                last_blocked = e
                if idx < len(attempts):
                    logger.warning(
                        "[%s] OurDomain 403，切换 TLS 指纹重试 %d/%d: %s",
                        display, idx + 1, len(attempts), attempts[idx],
                    )
                    continue
                raise BlockedError(
                    f"{e} 已尝试 TLS 指纹: {', '.join(tried)}。"
                    "如仍被挡，可配置 HTTPS_PROXY 换出口 IP，或用 "
                    "OURDOMAIN_IMPERSONATES 调整指纹顺序。"
                ) from e
            except RateLimitError:
                raise
            except Exception as e:
                raise ScrapeNetworkError(f"[{display}] OurDomain 抓取失败: {e}") from e
        else:
            if last_blocked is not None:
                raise last_blocked
            all_units, complete, fp_names_by_id = {}, False, {}

        listings = [
            _to_listing(
                unit,
                base_url=f"{base}/{slug}/floorplans.aspx",
                city_display=display,
                building_label=building.get("short_display") or task.extra.get("building_label"),
                source=self.source,
                default_type=building.get("type"),
                fp_names_by_id=fp_names_by_id,
                street_address=building.get("street_address"),
            )
            for unit in all_units.values()
        ]
        logger.info("[%s] OurDomain 共抓取 %d 个单元", display, len(listings))
        return ScrapeResult(task=task, listings=listings, complete=complete)

    def _scrape_once(
        self,
        *,
        display: str,
        base: str,
        floorplans_url: str,
        property_id: str,
        move_in_date: str,
        proxies: dict[str, str],
        impersonate: str,
    ) -> tuple[dict[str, dict], bool, dict[str, str]]:
        all_units: dict[str, dict] = {}
        complete = True
        with req.Session(impersonate=impersonate, proxies=proxies) as session:
            fp_html = _get_text(
                session,
                floorplans_url,
                headers=_headers_for(floorplans_url),
            )
            fp_ids = _extract_floorplan_ids(fp_html)
            if not fp_ids:
                logger.warning("[%s] OurDomain 未发现 floorplan id", display)
                return {}, False, {}

            # 顺手抓 FP id→name 映射，只作为 _infer_occupancy 的 sqft 兜底。
            # 主信号是 unit 自己的 sqft，所以这个 dict 拿不到也不影响。
            fp_names_by_id = _extract_floorplan_names(fp_html)
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "[%s] OurDomain FP names: %d / %d",
                    display, len(fp_names_by_id), len(fp_ids),
                )

            for fp_id in fp_ids:
                url = (
                    f"{base}/rcLoadContent.ashx"
                    f"?contentclass=availableunits"
                    f"&floorPlans={fp_id}"
                    f"&MoveInDate={move_in_date}"
                    f"&myolePropertyID={property_id}"
                )
                try:
                    unit_html = _get_text(
                        session,
                        url,
                        headers=_headers_for(url, referer=floorplans_url, ajax=True),
                    )
                except (RateLimitError, BlockedError):
                    raise
                except Exception as e:
                    complete = False
                    logger.error(
                        "[%s] OurDomain availableunits 失败 fp_id=%s: %s",
                        display, fp_id, e,
                        exc_info=True,
                    )
                    continue
                for unit in _extract_units(unit_html):
                    _merge_unit(all_units, unit, fp_id)
        return all_units, complete, fp_names_by_id

    def _building_for_task(self, task: ScrapeTask) -> dict[str, str]:
        key = (task.city_key or "").strip().lower()
        building = dict(self.BUILDINGS.get(key) or {})
        if not building:
            slug = task.extra.get("slug") or task.city_key
            property_id = task.extra.get("property_id")
            if not slug or not property_id:
                raise ValueError(
                    f"Unknown OurDomain city_key={task.city_key!r}; "
                    "provide extra.slug and extra.property_id"
                )
            building = {
                "slug": str(slug),
                "display": task.city_display,
                "property_id": str(property_id),
            }
        if task.extra.get("type"):
            building["type"] = str(task.extra["type"])
        return building


def _get_text(session: req.Session, url: str, *, headers: Optional[dict[str, str]] = None) -> str:
    """GET text with 429 retry and 403 Cloudflare classification."""
    total_wait = 0
    for attempt, wait in enumerate([0] + list(RATE_LIMIT_BACKOFF)):
        if wait:
            total_wait += wait
            logger.warning(
                "OurDomain 429，第 %d/%d 次退避，等待 %d 秒（累计 %ds）",
                attempt, len(RATE_LIMIT_BACKOFF), wait, total_wait,
            )
            time.sleep(wait)

        resp = session.get(url, headers=headers or {}, timeout=30)
        if resp.status_code == 403:
            body = resp.text[:500]
            is_cf = is_cloudflare_body(body)
            reason = "Cloudflare WAF 屏蔽" if is_cf else "服务拒绝"
            logger.warning(
                "OurDomain GET HTTP 403 (%s) url=%s body=%r",
                reason, url, body[:200],
            )
            raise BlockedError(
                f"OurDomain {reason}（HTTP 403）。等待无法恢复。请尝试："
                f"1) 更换 HTTPS_PROXY 出口 IP；"
                f"2) 重启 monitor（重建 curl_cffi session + TLS 指纹）；"
                f"3) 暂停几小时让 Cloudflare 冷却。"
            )
        if resp.status_code == 429:
            continue
        if not resp.ok:
            logger.error("OurDomain GET HTTP %d url=%s body=%r", resp.status_code, url, resp.text[:300])
        resp.raise_for_status()
        return resp.text

    raise RateLimitError(
        f"OurDomain 持续返回 429（已退避重试 {len(RATE_LIMIT_BACKOFF)} 次，"
        f"累计等待 {total_wait}s）。请降低轮询频率或配置 HTTPS_PROXY。"
    )


def _impersonate_attempts() -> list[str]:
    raw = os.environ.get("OURDOMAIN_IMPERSONATES", "")
    configured = [p.strip() for p in re.split(r"[,|]", raw) if p.strip()]
    candidates = configured or [get_impersonate(), *_DEFAULT_IMPERSONATES]
    unique = list(dict.fromkeys(candidates))
    retries = _env_int("OURDOMAIN_WAF_RETRIES", min(4, len(unique)), min_value=1, max_value=8)
    return unique[:retries]


def _env_int(name: str, default: int, *, min_value: int, max_value: int) -> int:
    try:
        value = int(os.environ.get(name, "") or default)
    except ValueError:
        value = default
    return max(min_value, min(value, max_value))


def _headers_for(url: str, *, referer: str = "", ajax: bool = False) -> dict[str, str]:
    headers = {
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8"
        ),
        "Accept-Language": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Upgrade-Insecure-Requests": "1",
    }
    if referer:
        headers["Referer"] = referer
    if ajax:
        headers.update({
            "Accept": "text/html, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
        })
    return headers


def _extract_floorplan_ids(html: str) -> list[str]:
    # 有 two 种 RENTCafe FP id 格式：
    #   subPointerId=NNNN          — photo gallery onclick（Diemen 在用）
    #   myFloorPlanId=NNNN         — Get Notified / Contact Us 链接（South-East 在用）
    ids = re.findall(r"subPointerId=(\d+)", html)
    if not ids:
        ids = re.findall(r"myFloorPlanId=(\d+)", html)
    return list(dict.fromkeys(ids))


def _extract_floorplan_names(html: str) -> dict[str, str]:
    """
    从 floorplans.aspx 解析 ``{fp_id: fp_name}``。

    实现踩坑记录
    ------------
    试过两种失败的方案：
    1. ``#FFloorPlan`` dropdown 的 checkbox label —— 那是 JS hydrated 的，
       curl_cffi 看不到（只有 Playwright 等 JS 跑完才能拿到）。
    2. anchor 的 ``title="..."`` 属性 —— Playwright dump 里有 FP 名，但
       production server-side HTML 里 title 全是 ``"1"`` / ``"Max Rent"``
       等 UI 占位文本，不含 FP 名（浏览器里 JS 后期填的 tooltip）。

    **真实可用 source**：每个 FP 的 floor-plan-selector anchor 都带：

        onclick="showDialog('Floor Plan Executive Studio | Furnished | Contract 1-5 years',
                            'photogallery',
                            'imagetype=floorplan&...&subPointerId=1106316&...');"

    第一个参数 ``'Floor Plan {name}'`` 是干净 FP 名，后面同一 onclick 里
    的 ``subPointerId=NNN`` 是对应 FP 的数字 ID。这两者**强耦合在同一 anchor**，
    server-side rendered，跟 ``_extract_floorplan_ids`` 用的 anchor 是一回事。

    FP 名字里含 Occupancy 线索（Studio / 1-Bedroom / "1-person max"）——
    SecureRC 单元表本身没有 Beds 字段，只能从这里反推。
    """
    mapping: dict[str, str] = {}
    # 'Floor Plan {name}' ... subPointerId=NNN 之间允许 onclick 内任意其它参数
    # （photogallery / imagetype / galleryId 等）。[^>]*? 限制不跨越 tag 边界，
    # 避免误匹配下一个 anchor 的 subPointerId。
    pattern = re.compile(
        r"showDialog\(\s*'Floor Plan ([^']+)'[^>]*?subPointerId=(\d+)",
        re.IGNORECASE | re.DOTALL,
    )
    for raw_name, fp_id in pattern.findall(html):
        if fp_id in mapping:
            continue
        name = unescape(raw_name).strip()
        if name:
            mapping[fp_id] = name
    return mapping


# ────────────────────────────────────────────────────────────────────
# Occupancy 反推：用 H2S 同样的词汇表，filter 跨 source 才能合并
# ────────────────────────────────────────────────────────────────────
#
# OD SecureRC 单元表没有 Beds / Occupancy 列。线索全在 Floor Plan 名字里：
#
#   "Executive Studio"        → 单人 studio（OD 营销定位 "Students"）
#   "Superior Plus Studio"    → 大 studio（OD 营销定位 "Young Pros & Couples"）
#   "Superior Studio"         → 单人版（"1-person max" 确认）
#   "1-Bedroom Apartment"     → 双人公寓（夫妻 / 情侣）
#   "1-Bedroom Loft"          → 双人 Loft
#   "2-Bedroom" / "3-Bedroom" → 家庭
#
# 优先级：显式 "1-person max" > "Plus Studio" > "Studio" > "1-Bedroom" > "2/3-Bedroom"

def _infer_occupancy(
    sqft: Optional[str] = None,
    fp_names: Optional[list[str]] = None,
) -> Optional[str]:
    """
    OurDomain unit → Occupancy 启发式。

    用 H2S 词汇表（"One" / "Two (only couples)" / "Family (parents with children)"），
    保证 Web `Occupancy` 多选 filter 跨 source 自然合并。

    主信号：unit 自己的物理面积 ``sqft``
    --------------------------------------
    OurDomain 的 RentCafe ``rcLoadContent.ashx?floorPlans=N`` 过滤器**不可靠**
    （实测每个 FP 查询都返回同一组单元，每个单元被关联到全部 8 个 FP，
    无法靠 FP→unit 映射判断单元类型）。

    所以改用 ``sqft`` 作为权威信号——它是 unit 的真实物理属性，从 unit
    table 行里直接抓到：

    - < 30 m²       → "One"（典型单人 studio）
    - 30 – 60 m²    → "Two (only couples)"（大 studio / 1-BR 公寓）
    - >= 60 m²      → "Family (parents with children)"（2+ BR）

    阈值取自荷兰租赁市场常规：单身 studio 18-28 m²，couple 30+ m²，
    家庭 60+ m²（与 H2S Eindhoven Kastanjelaan 59 m² Loft "Two (only couples)"
    一致）。

    兜底：fp_names
    --------------
    当 sqft 拿不到或非数字时，回退到 FP 名字关键词匹配。罕见路径——
    生产 OD 的 rcLoadContent 单元表 sqft 列稳定存在。
    """
    # 主路径：sqft
    if sqft:
        # 复用 models.parse_float（容忍 "22" / "22.5" / "38 - 48 m²" 等格式）
        from models import parse_float
        sqm = parse_float(sqft)
        if sqm is not None:
            if sqm < 30:
                return "One"
            if sqm < 60:
                return "Two (only couples)"
            return "Family (parents with children)"

    # 兜底：FP 名字（sqft 缺失时罕见路径）
    if fp_names:
        joined = " | ".join(fp_names).lower()
        if "1-person max" in joined or "1 person max" in joined:
            return "One"
        if re.search(r"\b[2-9]-bedroom", joined):
            return "Family (parents with children)"
        if "1-bedroom" in joined or "1 bedroom" in joined:
            return "Two (only couples)"
        if "studio" in joined:
            if "plus studio" in joined:
                return "Two (only couples)"
            return "One"

    return None


def _extract_units(html: str) -> list[dict]:
    units: list[dict] = []
    for row in re.finditer(
        r"<tr\b(?=[^>]*\bid=[\"']unitrow_(\d+)[\"'])[^>]*>.*?</tr>",
        html,
        re.IGNORECASE | re.DOTALL,
    ):
        unit = _extract_unit(row.group(0), row.group(1))
        if unit:
            units.append(unit)
    return units


def _extract_unit(row_html: str, unit_id: str) -> Optional[dict]:
    idx_match = re.search(
        r"data-selenium-id=[\"']urow(\d+)[\"']",
        row_html,
        re.IGNORECASE,
    )
    idx = idx_match.group(1) if idx_match else "1"

    apt = _cell_text(row_html, f"Apt{idx}")
    if not apt:
        logger.warning("OurDomain 单元缺少 Apt%s: unit_id=%s", idx, unit_id)
        return None

    detail_labels = _label_texts(_cell_html(row_html, f"Amenity{idx}"))
    detail = ", ".join(detail_labels)
    floor = parse_ourdomain_floor(detail)
    avail_date = _extract_apply_date(row_html)

    return {
        "unit_id": unit_id,
        "apt": apt,
        "sqft": _cell_text(row_html, f"SqFt{idx}"),
        "rent": _cell_text(row_html, f"Rent{idx}"),
        "deposit": _cell_text(row_html, f"Deposit{idx}"),
        "detail": detail,
        "floor": floor,
        "status": _extract_status(_cell_html(row_html, f"AvailDate{idx}")),
        "avail_date": avail_date,
        "fp_ids": [],
    }


def _merge_unit(all_units: dict[str, dict], unit: dict, fp_id: str) -> None:
    unit_id = unit["unit_id"]
    if unit_id not in all_units:
        all_units[unit_id] = unit
    if fp_id not in all_units[unit_id]["fp_ids"]:
        all_units[unit_id]["fp_ids"].append(fp_id)


def _to_listing(
    unit: dict,
    *,
    base_url: str,
    city_display: str,
    source: str,
    building_label: Optional[str] = None,
    default_type: Optional[str] = None,
    fp_names_by_id: Optional[dict[str, str]] = None,
    street_address: Optional[str] = None,
) -> Listing:
    apt = unit.get("apt") or f"#{unit['unit_id']}"
    detail = unit.get("detail") or "OurDomain"
    sqft = unit.get("sqft") or ""
    listing_name = _format_listing_name(city_display, apt, building_label)

    # 该单元归属的所有 FP 名字——只作 Occupancy 反推的 fallback。
    # 主信号是 unit 自己的 sqft（OurDomain rcLoadContent 的 FP 过滤器不可靠，
    # 每个单元都被关联到全部 8 FP，FP→unit 映射不能信）。
    fp_names: list[str] = []
    if fp_names_by_id:
        for fp_id in unit.get("fp_ids", []):
            name = fp_names_by_id.get(fp_id)
            if name:
                fp_names.append(name)
    occupancy = _infer_occupancy(sqft=sqft, fp_names=fp_names)

    features = [
        f"Unit: {apt}",
        f"Building: {city_display}",
    ]
    if street_address:
        # 真实街道地址（建筑级，所有 unit 共享）。供 geocode pipeline 用
        # `Address:` 优先级最高（unit name "Diemen #6045" 不可 geocode）。
        features.append(f"Address: {street_address}")
    if default_type:
        features.append(f"Type: {default_type}")
    if sqft:
        features.append(f"Area: {sqft} m²")
    if occupancy:
        # 用 H2S 同样的 "Occupancy: ..." 前缀写进 features，Web filter
        # `get_feature_values("Occupancy")` 会自动 distinct 出来
        features.append(f"Occupancy: {occupancy}")
    if unit.get("floor") is not None:
        features.append(f"Floor: {unit['floor']}")
    if unit.get("deposit"):
        features.append(f"Deposit: {unit['deposit']}")
    if detail:
        features.append(f"Detail: {detail}")
    if unit.get("fp_ids"):
        features.append(f"Floorplans: {', '.join(unit['fp_ids'])}")

    return Listing(
        id=f"od_{unit['unit_id']}",
        name=listing_name,
        status=unit.get("status") or "Occupied",
        price_raw=unit.get("rent") or None,
        available_from=unit.get("avail_date") or None,
        features=features,
        url=base_url,
        city=city_display,
        source=source,
    )


def _format_listing_name(
    city_display: str,
    apt: str,
    building_label: Optional[str] = None,
) -> str:
    building = (building_label or _short_building_label(city_display)).strip()
    unit = (apt or "").strip()
    if not unit.startswith("#") and unit:
        unit = f"#{unit}"
    return " ".join(part for part in [building, unit] if part)


def _short_building_label(city_display: str) -> str:
    value = (city_display or "").strip()
    if not value:
        return "OurDomain"
    lower = value.lower()
    if lower == "amsterdam diemen" or lower.endswith(" diemen"):
        return "Diemen"
    return value


def _cell_html(row_html: str, selenium_id: str) -> str:
    pattern = (
        r"<(?P<tag>th|td)\b"
        rf"(?=[^>]*data-selenium-id=[\"']{re.escape(selenium_id)}[\"'])"
        r"[^>]*>(?P<body>.*?)</(?P=tag)>"
    )
    m = re.search(pattern, row_html, re.IGNORECASE | re.DOTALL)
    return m.group("body") if m else ""


def _cell_text(row_html: str, selenium_id: str) -> str:
    return _strip_html(_cell_html(row_html, selenium_id))


def _strip_html(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", unescape(value)).strip()


def _label_texts(html: str) -> list[str]:
    labels: list[str] = []
    for m in re.finditer(r"<label\b[^>]*>(.*?)</label>", html, re.IGNORECASE | re.DOTALL):
        text = _strip_html(m.group(1))
        if not text:
            continue
        lower = text.lower()
        if lower == "max-rent" or "prices and special offers" in lower:
            continue
        labels.append(text)
    return labels


def _extract_status(avail_html: str) -> str:
    m = re.search(
        r"<span\b[^>]*class=[\"']([^\"']*)[\"'][^>]*>(.*?)</span>",
        avail_html,
        re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return "Occupied"
    classes = m.group(1).lower()
    text = _strip_html(m.group(2)).lower()
    if "success" in classes or text == "available":
        return "Available to book"
    if "warning" in classes or "wait" in text:
        return "Available in lottery"
    return "Occupied"


def _extract_apply_date(row_html: str) -> Optional[str]:
    m = re.search(
        r"ApplyNowClick\([^)]*?[\"'](\d{1,2}-\d{1,2}-\d{4})[\"']",
        row_html,
        re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return None
    return _normalize_date(m.group(1))


def _normalize_date(value: str) -> Optional[str]:
    value = value.strip()
    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})-(\d{4})", value)
    if m:
        day, month, year = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return f"{year:04d}-{month:02d}-{day:02d}"
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", value)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return None


def parse_ourdomain_floor(detail: str) -> Optional[int]:
    """Extract the lowest floor number from an OurDomain amenity label."""
    if not detail:
        return None
    lower = detail.lower()
    if "ground" in lower:
        return 0
    m = re.search(r"Floor\s*(\d+)", detail, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _next_month_first() -> str:
    today = date.today()
    year = today.year + (today.month // 12)
    month = (today.month % 12) + 1
    return f"{year:04d}-{month:02d}-01"
