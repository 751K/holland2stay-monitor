"""
scrapers/holland2stay.py — Holland2Stay 抓取实现
=================================================

把已经稳定运行的 H2S 抓取逻辑（``scraper.py``）封进 ``AbstractScraper``
子类。**逻辑零变更**——直接转发给 ``scraper._scrape_city_pages`` 内部函数，
保证现网行为不变。

后续如果要把整个 GraphQL pipeline 搬过来，可以慢慢迁移；当前这一层只
负责把 H2S 适配进 ScrapeTask / ScrapeResult 协议。
"""
from __future__ import annotations

import logging

import curl_cffi.requests as req

from config import get_impersonate, get_proxy_url
from models import Listing

from .base import (
    AbstractScraper,
    ScrapeResult,
    ScrapeTask,
)


logger = logging.getLogger(__name__)


class HollandStayScraper(AbstractScraper):
    """
    Holland2Stay GraphQL 抓取器。

    复用现有 ``scraper.py:_scrape_city_pages`` 实现——只是套了一层
    ``AbstractScraper`` 接口。Session 在每次 ``scrape()`` 时新建一次
    （延续原行为：每轮 scrape_all 一个 Session）。
    """

    source = "holland2stay"

    def scrape(self, task: ScrapeTask) -> ScrapeResult:
        # 延迟 import 避免 scrapers 包 -> scraper.py -> scrapers 包的循环
        # （scraper.py 在 P0 改造后仅做 re-export，理论上无循环，但保险）
        from scraper import _scrape_city_pages  # type: ignore

        availability_ids = task.extra.get("availability_ids") or ["179", "336"]

        proxy = get_proxy_url()
        proxies = {"https": proxy, "http": proxy} if proxy else {}

        with req.Session(impersonate=get_impersonate(), proxies=proxies) as session:
            listings, complete = _scrape_city_pages(
                session,
                task.city_display,
                city_ids=[task.city_key],
                availability_ids=availability_ids,
            )

        for l in listings:
            l.source = self.source

        logger.info("[%s] Holland2Stay 共抓取 %d 条房源", task.city_display, len(listings))
        return ScrapeResult(
            task=task,
            listings=listings,
            complete=complete,
        )
