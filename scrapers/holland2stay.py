"""
scrapers/holland2stay.py — Holland2Stay 适配层（**逻辑不在这**）
================================================================

⚠️ 真正的 H2S 抓取逻辑全在 ``scraper.py``（654 行）：GraphQL 请求 + 403/
维护探测 + 翻页 + 解析。本文件只是把那套逻辑**适配**进 ``AbstractScraper``
/ ``ScrapeTask`` / ``ScrapeResult`` 协议，外加管理一个批次级 Session。

为什么不合并
------------
``scraper.py`` 是最 load-bearing 的赚钱代码、零 bug、被一堆测试按私有符号
（``_post_gql`` / ``_scrape_city_pages`` / ``_to_listing`` …）直接引用。为了
"命名一致性"搬 654 行最关键代码，是拿稳定性换整洁——不划算。等下次因别的
原因要大改 ``scraper.py`` 时再顺手 fold 进来。

找 H2S 逻辑请去 ``scraper.py``；本文件只负责协议适配 + Session 生命周期。
"""
from __future__ import annotations

import logging
from contextlib import contextmanager

import curl_cffi.requests as req

from config import get_impersonate, get_proxy_url
from models import Listing

from .base import (
    AbstractScraper,
    ScrapeResult,
    ScrapeTask,
)


logger = logging.getLogger(__name__)


def _make_session() -> req.Session:
    """新建一个 curl_cffi Session（固定一个 TLS 指纹 + 代理）。"""
    proxy = get_proxy_url()
    proxies = {"https": proxy, "http": proxy} if proxy else {}
    return req.Session(impersonate=get_impersonate(), proxies=proxies)


class HollandStayScraper(AbstractScraper):
    """
    Holland2Stay GraphQL 抓取器。

    复用现有 ``scraper.py:_scrape_city_pages`` 实现——只是套了一层
    ``AbstractScraper`` 接口。

    会话复用
    --------
    dispatcher 一次抓取多个城市时，会先进入 ``batch_session()`` 上下文，
    本类在此建**一个** Session（一个 TLS 指纹 + 一次握手），批次内所有
    城市复用它——恢复 P0 之前 ``scrape_all`` 的 1 Session/批次行为，避免
    N 个城市 = N 次握手 + N 个不同指纹（后者是 Cloudflare 的 bot 信号）。

    若不在批次上下文里（独立调用 ``scrape()``，例如单测），则按需自建并
    在结束后关闭，保持向后兼容。
    """

    source = "holland2stay"

    def __init__(self) -> None:
        # 批次作用域内的共享 Session；None 表示当前不在批次中。
        self._batch_session: req.Session | None = None

    @contextmanager
    def batch_session(self):
        """整批一个 Session + 一个固定 TLS 指纹（见类 docstring）。"""
        with _make_session() as session:
            self._batch_session = session
            try:
                yield
            finally:
                self._batch_session = None

    def scrape(self, task: ScrapeTask) -> ScrapeResult:
        # 延迟 import 避免 scrapers 包 -> scraper.py -> scrapers 包的循环
        # （scraper.py 在 P0 改造后仅做 re-export，理论上无循环，但保险）
        from scraper import _scrape_city_pages  # type: ignore

        availability_ids = task.extra.get("availability_ids") or ["179", "336"]

        if self._batch_session is not None:
            # 批次内：复用共享 Session（无握手开销，固定指纹）
            listings, complete = _scrape_city_pages(
                self._batch_session,
                task.city_display,
                city_ids=[task.city_key],
                availability_ids=availability_ids,
            )
        else:
            # 独立调用（单测 / 非 dispatcher 路径）：按需自建会话
            with _make_session() as session:
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
