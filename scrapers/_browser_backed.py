"""
scrapers/_browser_backed.py — 浏览器型 scraper 的共享生命周期
================================================================

H2S 与 Xior 都得靠 ``BrowserFetcher`` 过 Cloudflare 挑战，两边的浏览器管理
（懒创建 / 超龄重建 / 失效丢弃 / 批次作用域）此前是**两份 75% 重复的拷贝**，
连注释都是同一段。差别只有三处：用哪个 ``SiteProfile``、活多久、创建成功后
要不要额外重置点什么。

放在独立模块而不是 ``scrapers/base.py``：``base`` 是 source-agnostic 的抽象层，
OurDomain / OurCampus 走纯 HTTP，不该因为另外两家用浏览器就被拖进对
``browser_fetcher`` 的依赖。

线程模型
--------
Playwright 对象**绑定创建它的线程**。每个浏览器型 source 恒定跑在
``monitor._get_browser_executor(source)`` 的专属长存单线程上，且两个独立的
Playwright sync 实例不能共存于同一线程——所以 executor 必须按 source 分开。
本类不做任何加锁：dispatcher 逐 source 串行调用，批次内无并发。
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Optional

from browser_fetcher import BrowserFetcher, SiteProfile

from .base import AbstractScraper

logger = logging.getLogger(__name__)


class BrowserBackedScraper(AbstractScraper):
    """持有一个跨轮复用的 ``BrowserFetcher`` 的 scraper 基类。

    子类必须设
    ----------
    ``source``            见 ``AbstractScraper``
    ``_BROWSER_PROFILE``  该站点的 ``SiteProfile``
    ``_BROWSER_MAX_AGE``  浏览器最大存活秒数（超过则主动重建）

    子类可覆盖的钩子（默认全是 no-op）
    ----------------------------------
    ``_on_browser_ready()``   浏览器建好且过完挑战之后。用于重置挂在浏览器
                              生命周期上的派生状态（H2S 的 attr 标签表）。
    ``_on_browser_closed()``  浏览器关掉之后。同上，反向。
    ``_begin_batch()``        批次开头（``batch_session`` 进入，浏览器已就绪）。
    ``_end_batch()``          批次结尾（正常退出时；``yield`` 抛异常则不调用，
                              那条路由 dispatcher 记录）。

    浏览器生命周期
    --------------
    **跨轮复用**——首轮创建，之后复用同一个实例，避免每轮重跑一遍 CF 挑战
    （冷启动开销 + 挑战过于频繁本身就是 bot 信号）。

    关闭重建只在三种情况发生：

    - 超过 ``_BROWSER_MAX_AGE`` → ``_ensure_browser()`` 主动重建
    - 本批次出现过 403 或未预期异常 → dispatcher 调 ``invalidate_session()``
    - 进程退出

    ``batch_session()`` **不**创建/关闭浏览器，只负责让 dispatcher 拿到共享实例。
    它也**不**捕获抓取期的异常：dispatcher 是按 task 隔离的，``scrape()`` 抛的
    东西根本到不了 ``yield``——两边曾经各写过一段 ``except BlockedError:
    self._close_browser()``，都是死代码，「403 后关闭浏览器下轮重建」实际从未
    发生过。现在统一由 dispatcher 在批次结束后调 ``invalidate_session()``。
    """

    #: 子类必须覆盖
    _BROWSER_PROFILE: Optional[SiteProfile] = None
    _BROWSER_MAX_AGE: float = 7200.0

    def __init__(self) -> None:
        self._fetcher: Optional[BrowserFetcher] = None
        self._browser_created_at: float = 0.0
        self._browser_create_count: int = 0

    # ── 生命周期 ────────────────────────────────────────────────────

    def _ensure_browser(self) -> BrowserFetcher:
        """懒创建或复用浏览器实例。

        只在两种情况下真正重建：实例还没有，或已超过 ``_BROWSER_MAX_AGE``。
        抓取期的 403 由 dispatcher 在批次结束后调 ``invalidate_session()``
        丢弃会话，下一轮再走到这里时自然重建。
        """
        from config import CLOAKBROWSER_HEADLESS

        now = time.monotonic()
        if self._fetcher is not None:
            # 存活性排在年龄前面：进程没了的话，「还没到重建时间」毫无意义。
            # getattr 兜底是给测试替身留的；真实类必须有这个属性，由
            # tests/test_scraper_dispatch.py 的守卫钉住。
            if not getattr(self._fetcher, "is_alive", True):
                logger.warning(
                    "%s 浏览器已失联（进程退出或页面关闭），丢弃重建。"
                    "没有这道检查的话，死掉的实例会被一直复用到 %.0f 分钟的"
                    "年龄上限才自愈，中间每一轮都以同样方式失败。",
                    self.source, self._BROWSER_MAX_AGE / 60,
                )
                self._close_browser()
            elif now - self._browser_created_at > self._BROWSER_MAX_AGE:
                logger.info(
                    "%s 浏览器已存活 %.0f 分钟，主动重建",
                    self.source, (now - self._browser_created_at) / 60,
                )
                self._close_browser()
            else:
                return self._fetcher

        self._fetcher = self._new_fetcher(headless=CLOAKBROWSER_HEADLESS)
        try:
            self._fetcher.__enter__()
            self._fetcher.ensure_initialized()
            self._browser_created_at = time.monotonic()
            self._browser_create_count += 1
            self._on_browser_ready()
            logger.info(
                "%s 浏览器已创建并完成 CF 挑战（第 %d 次）",
                self.source, self._browser_create_count,
            )
            return self._fetcher
        except Exception:
            self._close_browser()
            raise

    def _close_browser(self) -> None:
        """关闭浏览器，释放资源。由 ``invalidate_session()`` 和超龄重建调用。"""
        if self._fetcher is not None:
            try:
                self._fetcher.__exit__(None, None, None)
            except Exception:
                pass
            self._fetcher = None
            self._on_browser_closed()

    def invalidate_session(self) -> None:
        """丢弃浏览器——坏掉的会话留着会让后续每轮重复失败。"""
        self._close_browser()

    @contextmanager
    def batch_session(self):
        """批次作用域：确保浏览器存活，dispatcher 通过此入口拿到共享实例。"""
        self._ensure_browser()
        self._begin_batch()
        yield
        self._end_batch()

    # ── 钩子 ─────────────────────────────────────────────────────────

    def _new_fetcher(self, *, headless: bool) -> BrowserFetcher:
        """建一个 fetcher。**子类要覆盖，而且要引用自己模块里的 ``BrowserFetcher``。**

        看着像多余的一行，但它是**测试的接缝**：用例一直是
        ``monkeypatch.setattr(scrapers.holland2stay, "BrowserFetcher", Fake)``
        这么打桩的。若这里直接用本模块导入的名字，那些桩会全部失效——而失效的
        后果不是测试报错，是**测试真的去启动一个 Chromium**（迁移过程中实测到
        了，cloakbrowser 的升级提示直接打进了测试输出）。

        所以接缝留在子类：生命周期逻辑共享，「用哪个类、哪个 profile」由子类说。
        """
        if self._BROWSER_PROFILE is None:
            raise NotImplementedError(
                f"{type(self).__name__} 既没覆盖 _new_fetcher 也没设 _BROWSER_PROFILE"
            )
        return BrowserFetcher(headless=headless, profile=self._BROWSER_PROFILE)

    def _on_browser_ready(self) -> None:
        """浏览器建好之后。重置挂在浏览器生命周期上的派生状态。"""

    def _on_browser_closed(self) -> None:
        """浏览器关掉之后。同上，反向。"""

    def _begin_batch(self) -> None:
        """批次开头（浏览器已就绪）。"""

    def _end_batch(self) -> None:
        """批次正常结尾。``yield`` 抛异常时不调用——那条路由 dispatcher 记录。"""
