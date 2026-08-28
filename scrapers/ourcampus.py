"""
scrapers/ourcampus.py — OurCampus scraper
==========================================

OurCampus 是 Greystar 的另一个学生住房品牌，与 OurDomain 同属一家、**同一套
后端**（Webflow 前台 + RentCafe/SecureRC 后台）。因此本 scraper 直接继承
``OurDomainScraper``，复用它的全部机制：

- TLS 指纹池 + 冷却状态机（``_FINGERPRINT_STATE`` 是模块级的，两个 source 共享
  ——它们打的是同一个 SecureRC 集群，某个指纹被烧对两边同时生效）
- 每次尝试换出口 IP（``rotating=True``）
- 同 session 内 403 重试
- floorplans.aspx 发现 + 单元行解析（``data-selenium-id`` / ``data-label`` 双策略）
- 状态映射、Occupancy 反推、Listing 映射

唯一需要覆盖的是**取单元表的请求形状**，见 ``_fetch_units_html``。

规模与预期
----------
只有一栋楼（Amsterdam Diemen，Dalsteindreef 6002），三个房型。官网自述等待期
16–18 个月，属于排队制而非先到先得，所以「秒级通知」在这里的价值远低于 H2S。
接入是**产品决策**，不是因为投入产出比划算——文档里对它的评估见
``docs/SCRAPING_RECON.md`` §4。

验证状态
--------
接入时该楼**零可订单元**，因此单元表的 HTML 结构当时没有真实样本可核对。
当时判断「复用 OurDomain 的解析器」成立的依据是：两边的**空响应结构指纹完全
一致**（``Apartment Search Result`` 面板 + ``innerformdiv`` + ``myOlePropertyId``，
都无 unitrow），说明是同一套 RentCafe 模板。

真实样本已于 2026-08-10 首次出现，并在 2026-08-27 用于核对解析器。核对结果是
模板判断成立，但**状态映射不成立**：feed 并不只列出可订单元，且置灰的日期单元
格表示「自该日起可订」而非「已出租」。该错误曾使整批可订单元被判为 Occupied
且不发任何通知，修正见 CHANGELOG v1.26.0——判据现依据该行是否带有可用的下单
按钮，未识别的样式类放行并告警。

结构真的不同时，基类的完整性守卫会兜住：解析不出单元且响应又不像单元面板时
标记 incomplete，而不是误报「没有房」。但如果结构不同却仍是合法面板，仍会静默
返回 0 个单元，所以留档不能停——每次请求都往 ``data/ourcampus_capture.txt``
记一行摘要，并在**有单元或疑似解析失配时**附完整 HTML。
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import curl_cffi.requests as req

from .base import ScrapeTask
from .ourdomain import (
    OurDomainScraper,
    _extract_units,
    _get_text,
    _headers_for,
    _looks_like_availability_panel,
)

logger = logging.getLogger(__name__)


# ── 抓取留档 ────────────────────────────────────────────────────────
#
# 存在的唯一理由：**它的单元表 HTML 至今没有真实样本**（见模块文档）。等到这栋
# 楼第一次真的有房时，需要拿原始 markup 核对解析器——只看日志里的「共抓取 N 个
# 单元」不够，因为最危险的情况恰恰是「结构变了但仍是合法面板」，那会静默返回 0。
#
# 所以：每次请求都记一行摘要，**只在有看头的时候**才附完整 HTML：
#   - 解析出单元了 → 第一份真实样本，必须留
#   - 响应里有 unitrow 痕迹但解析出 0 个 → 正是解析器对不上的信号，更要留
# 平时（零可订）只有摘要行，一天几百轮也就几十 KB。
_CAPTURE_PATH_ENV = "OURCAMPUS_CAPTURE_PATH"
_CAPTURE_MAX_BYTES = 8 * 1024 * 1024
_UNITROW_HINT = re.compile(r'id=["\']unitrow_\d+', re.IGNORECASE)


def _capture_path() -> Path:
    override = os.environ.get(_CAPTURE_PATH_ENV, "").strip()
    if override:
        return Path(override)
    from config import DATA_DIR  # 延迟导入，避免 config ↔ scrapers 循环
    return DATA_DIR / "ourcampus_capture.txt"


def _record_capture(fp_id: str, html: str) -> None:
    """把一次 availableunits 响应记进留档文件。任何异常都不许影响抓取。"""
    try:
        path = _capture_path()
        if path.exists() and path.stat().st_size > _CAPTURE_MAX_BYTES:
            return  # 满了就停，不轮转——这是排查用的一次性证据，不是运行日志

        parsed = len(_extract_units(html))
        has_rows = bool(_UNITROW_HINT.search(html or ""))
        panel = _looks_like_availability_panel(html)
        interesting = parsed > 0 or has_rows

        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        head = (
            f"=== {stamp}  fp={fp_id}  bytes={len(html or '')}  "
            f"panel={'yes' if panel else 'NO'}  unitrow={'yes' if has_rows else 'no'}  "
            f"parsed={parsed} ===\n"
        )
        body = ""
        if interesting:
            body = (
                "--- 完整响应（首次出现单元 / 解析器可能对不上，留作核对）---\n"
                + (html or "") + "\n--- 响应结束 ---\n"
            )
            if parsed == 0:
                logger.warning(
                    "OurCampus 响应含 unitrow 但解析出 0 个单元——解析器可能与"
                    "该主题不匹配，原始 HTML 已留档到 %s", path,
                )
            else:
                logger.info(
                    "OurCampus 首次解析出 %d 个单元，原始 HTML 已留档到 %s",
                    parsed, path,
                )

        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(head + body)
    except Exception:
        logger.debug("OurCampus 抓取留档失败（已忽略）", exc_info=True)


class OurCampusScraper(OurDomainScraper):
    """Unit-level scraper for OurCampus (Greystar / RentCafe)."""

    source = "ourcampus"

    # 独立前缀：OurCampus（property 186609）与 OurDomain（184283 / 182801）
    # 是不同的 RentCafe property，unit id 跨 property 是否唯一没有保证。
    ID_PREFIX = "oc_"

    BASE = (
        "https://new-ourcampus-amsterdam-diemen-rentcafewebsiteuk"
        ".securerc.co.uk/onlineleasing"
    )

    BUILDINGS: dict[str, dict[str, str]] = {
        "diemen": {
            "slug": "new-ourcampus-amsterdam-diemen",
            "display": "OurCampus Amsterdam Diemen",
            "short_display": "OurCampus Diemen",
            "property_id": "186609",
            "type": "Studio",
            # 建筑级真实街道地址，供 geocode 用（unit 名不可 geocode）。
            # 与 OurDomain South-East（Dalsteindreef 20-40）同街，是隔壁楼。
            "street_address": "Dalsteindreef 6002, 1112 XC Diemen",
            # 纯学生盘，整栋一个值，不按面积切。ourcampus.nl/en/criteria：
            # 「Enrolled at an MBO, HBO, or university-level institution
            # (only universities located in the Netherlands)」+ 在校证明，
            # PhD / 博后明确不符合，且无收入要求。页脚写着「Young
            # professionals book at: [OurDomain]」——它把有收入的人劝去隔壁。
            "tenant_policy": {"default": "student only"},
        },
    }

    def _fetch_units_html(
        self,
        session: req.Session,
        *,
        base: str,
        fp_id: str,
        property_id: str,
        move_in_date: str,
        floorplans_url: str,
    ) -> str:
        """POST + ``floorPlans[]`` 表单体，照抄它自己前端的调用。

        OurCampus 的 floorplans.aspx 里是这么发的::

            $('#FloorPlanContainer').parent().load(
                '/onlineleasing/rcLoadContent.ashx?contentclass=availableunits&t=' + Math.random(),
                {'floorPlans': names})

        jQuery 的 ``.load(url, obj)`` 在 data 是对象时走 **POST**，数组序列化成
        ``floorPlans[]``；``MoveInDate`` / ``myolePropertyID`` 都不传，由会话隐含。

        为什么不沿用基类的 GET：接入时该楼零可订，GET 和 POST 都只能拿到空面板，
        **无法用响应区分两者对错**。所以选了「和它自己前端一致」这条——那是唯一
        有证据支持的形状。基类的 GET 形状对 OurDomain 是实测有效的，不动。

        另注：OurDomain 的 host 上 POST 会触发 403，OurCampus 的 host 不会
        （实测 HTTP 200）。同一套 RentCafe，WAF 策略按 host 配。
        """
        url = f"{base}/rcLoadContent.ashx?contentclass=availableunits"
        html = _get_text(
            session, url,
            headers=_headers_for(url, referer=floorplans_url, ajax=True),
            data=[("floorPlans[]", str(fp_id))],
        )
        _record_capture(str(fp_id), html)
        return html

    def _building_for_task(self, task: ScrapeTask) -> dict[str, str]:
        # 基类的报错文案写死了 "OurDomain"，这里换成本 source 的名字，
        # 免得配置写错时把人指向另一个平台的文档。
        key = (task.city_key or "").strip().lower()
        if key not in self.BUILDINGS and not (
            task.extra.get("slug") and task.extra.get("property_id")
        ):
            raise ValueError(
                f"Unknown OurCampus city_key={task.city_key!r}；"
                f"已知 key: {sorted(self.BUILDINGS)}，"
                "或提供 extra.slug + extra.property_id"
            )
        return super()._building_for_task(task)
