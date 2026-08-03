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

未验证的部分
------------
接入时该楼**零可订单元**，所以它的单元表 HTML 结构没有被真实样本验证过。
判断「复用 OurDomain 的解析器」成立的依据是：两边的**空响应结构指纹完全一致**
（``Apartment Search Result`` 面板 + ``innerformdiv`` + ``myOlePropertyId``，
都无 unitrow），说明是同一套 RentCafe 模板。

万一它的单元行结构真的不同，基类的完整性守卫会兜住——解析不出单元且响应又不
像单元面板时标记 incomplete，而不是误报「没有房」。但如果结构不同却仍是合法
面板，就会静默返回 0 个单元。**首次真实有房时应人工核对一次日志。**
"""
from __future__ import annotations

import logging

import curl_cffi.requests as req

from .base import ScrapeTask
from .ourdomain import OurDomainScraper, _get_text, _headers_for

logger = logging.getLogger(__name__)


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
        return _get_text(
            session, url,
            headers=_headers_for(url, referer=floorplans_url, ajax=True),
            data=[("floorPlans[]", str(fp_id))],
        )

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
