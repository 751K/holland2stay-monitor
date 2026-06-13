"""
mcore/browser_fetcher.py — 共享 CloakBrowser 管理 + GraphQL fetch
==================================================================

为 scraper 和 booker 提供统一的浏览器内 GraphQL 请求能力。

浏览器内 fetch() 自动携带所有 cookies / TLS 指纹 / CF clearance token，
无需手动管理会话。CF Turnstile 挑战在首次请求时自动完成。

线程安全
--------
每个 BrowserFetcher 实例绑定单线程——Scraper 在 executor 线程内用，
Booker 在 ThreadPoolExecutor 线程内用，各自独立实例，无共享状态。
"""
from __future__ import annotations

import logging
import platform
import time
from typing import Optional
from json import dumps as _json_dumps

logger = logging.getLogger(__name__)

# ── 延迟导入异常类 ──────────────────────────────────────────────────
# browser_fetcher 被 scrapers/holland2stay 导入，而 scrapers.base（定义
# 异常的模块）通过 scrapers/__init__.py 提前触发了 holland2stay 的加载。
# 若在模块顶层 import scrapers.base 会导致循环导入。
# 异常只在 fetch_gql 实际遇到错误时才需要，故延迟到首次 raise 时加载。
_exc_cache: dict[str, type] = {}


def _exc(name: str) -> type:
    if name not in _exc_cache:
        from scrapers.base import (  # noqa: E402
            BlockedError,
            RateLimitError,
            ScrapeNetworkError,
            UpstreamMaintenanceError,
        )
        _exc_cache.update({
            "BlockedError": BlockedError,
            "RateLimitError": RateLimitError,
            "ScrapeNetworkError": ScrapeNetworkError,
            "UpstreamMaintenanceError": UpstreamMaintenanceError,
        })
    return _exc_cache[name]


_H2S_MAIN_PAGE = "https://www.holland2stay.com/residences"
_H2S_GQL_PATH = "/api/graphql"
_H2S_GQL_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    # Match Magento/Hyva-style storefront GraphQL requests more closely than a
    # bare browser fetch. These are not secrets and are safe for anonymous reads.
    "Store": "default",
    "Content-Currency": "EUR",
}


class BrowserFetcher:
    """
    管理 CloakBrowser 生命周期，提供 ``fetch_gql()`` 在浏览器内发 GraphQL 请求。

    用法
    ----
    ::

        with BrowserFetcher(headless=True) as fetcher:
            data = fetcher.fetch_gql(query, variables)
            auth_data = fetcher.fetch_gql(mutation, vars, extra_headers={"Authorization": "Bearer xxx"})

    资源
    ----
    空闲 ~190MB，3 个 tab ~280MB。使用完后必须 close() 或通过上下文管理器释放。
    """

    def __init__(self, headless: bool = True):
        self._headless = headless
        self._browser = None
        self._page = None
        self._initialized = False
        self._effective_headless = headless

    # ── 上下文管理器 ──────────────────────────────────────────────────
    def __enter__(self) -> "BrowserFetcher":
        from cloakbrowser import launch

        if platform.system() == "Darwin" and self._effective_headless:
            logger.info("macOS 本地 CloakBrowser 使用 headed 调试模式，避免 headless SIGABRT")
            self._effective_headless = False

        # Docker/Linux 兼容参数：
        # - disable-dev-shm-usage: /dev/shm 默认 64MB，Chromium 会崩，改用 /tmp
        # - disable-gpu: headless 不需要 GPU 加速，避免无 GPU 环境报错
        #
        # macOS 本地 CloakBrowser headless v145 存在 SIGABRT 风险；这些 Linux
        # 参数也不该无条件注入到本地调试浏览器。
        chromium_args = []
        if platform.system() == "Linux":
            chromium_args = ["--disable-dev-shm-usage", "--disable-gpu"]

        self._browser = launch(
            headless=self._effective_headless,
            humanize=True,
            args=chromium_args,
        )
        self._page = self._browser.new_page()
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        """关闭浏览器，释放资源。"""
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
            self._page = None
            self._initialized = False

    # ── CF 挑战初始化 ─────────────────────────────────────────────────
    def ensure_initialized(self) -> None:
        """
        懒加载：首次请求前访问主页完成 CF Turnstile 挑战。

        公开方法——booker 可以提前调用来热身，也可以在 fetch_gql 首次调用时自动触发。
        """
        if self._initialized:
            return

        logger.info("CloakBrowser 加载主站完成 CF 挑战...")
        start = time.monotonic()
        try:
            self._page.goto(
                _H2S_MAIN_PAGE,
                wait_until="domcontentloaded",
                timeout=30_000,
            )
        except Exception as e:
            logger.error("主站加载失败: %s", e)
            raise _exc("ScrapeNetworkError")(
                f"H2S 主站加载失败（CF 挑战可能未通过）: {e}"
            ) from e

        self._raise_if_maintenance_page()

        # 等待 filter UI 出现，确认 CF 挑战完成 + React 已渲染
        try:
            self._page.wait_for_selector(
                '[data-cy="FilterList-item"]', timeout=25_000
            )
        except Exception:
            logger.warning("filter UI 未在预期时间内出现，继续尝试...")

        elapsed = time.monotonic() - start
        logger.info("CF 挑战完成 (%.1fs)", elapsed)
        self._initialized = True

    def _raise_if_maintenance_page(self) -> None:
        """浏览器已打开主站时，识别 H2S 维护页并走维护冷却分支。"""
        try:
            title = (self._page.title() or "").strip()
        except Exception:
            title = ""
        if "maintenance" in title.lower():
            raise _exc("UpstreamMaintenanceError")(
                f"H2S 平台维护中（页面标题: {title}）"
            )

        try:
            html = self._page.content()[:4000]
        except Exception:
            html = ""
        if not html:
            return
        from scrapers.base import is_maintenance_body  # 延迟导入，避免循环导入

        if is_maintenance_body(html):
            raise _exc("UpstreamMaintenanceError")("H2S 平台维护中")

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    # ── GraphQL fetch ──────────────────────────────────────────────────
    def fetch_gql(
        self,
        query: str,
        variables: dict | None = None,
        *,
        timeout_ms: int = 30_000,
        extra_headers: dict[str, str] | None = None,
    ) -> dict:
        """
        在浏览器内通过 fetch() 发 GraphQL POST 请求。

        Parameters
        ----------
        query         : GraphQL query 或 mutation 字符串
        variables     : GraphQL variables dict（可选）
        timeout_ms    : fetch 超时毫秒数
        extra_headers : 额外 HTTP 头（e.g. Authorization: Bearer xxx）

        Returns
        -------
        响应 JSON 的完整 dict（含 data / errors 字段）

        Raises
        ------
        BlockedError          HTTP 403 (CF 再次拦截)
        RateLimitError        HTTP 429 (限流)
        ScrapeNetworkError    网络/超时错误 / 非 JSON 响应
        """
        self.ensure_initialized()

        headers = dict(_H2S_GQL_HEADERS)
        if extra_headers:
            headers.update(extra_headers)

        body = _json_dumps({"query": query, "variables": variables or {}})
        headers_json = _json_dumps(headers)
        body_json = _json_dumps(body)

        js_code = f"""
            async () => {{
                const controller = new AbortController();
                const timeout = setTimeout(() => controller.abort(), {timeout_ms});
                try {{
                    const resp = await fetch('{_H2S_GQL_PATH}', {{
                        method: 'POST',
                        credentials: 'include',
                        mode: 'same-origin',
                        cache: 'no-store',
                        redirect: 'follow',
                        referrer: window.location.href,
                        referrerPolicy: 'strict-origin-when-cross-origin',
                        headers: {headers_json},
                        body: {body_json},
                        signal: controller.signal,
                    }});
                    clearTimeout(timeout);
                    const text = await resp.text();
                    const headers = {{}};
                    for (const [key, value] of resp.headers.entries()) {{
                        const lower = key.toLowerCase();
                        if (['cf-ray', 'content-type', 'server', 'vary'].includes(lower)) {{
                            headers[lower] = value;
                        }}
                    }}
                    return {{ status: resp.status, ok: resp.ok, text: text, headers: headers }};
                }} catch (err) {{
                    clearTimeout(timeout);
                    return {{ error: err.message || String(err) }};
                }}
            }}
        """
        result = self._page.evaluate(js_code)

        if "error" in result:
            raise _exc("ScrapeNetworkError")(f"浏览器内 fetch 失败: {result['error']}")

        status = result["status"]
        if status == 403:
            logger.warning(
                "GraphQL 返回 403，尝试重建 CF 会话... headers=%s body=%s",
                result.get("headers", {}),
                result.get("text", "")[:300],
            )
            self._initialized = False
            try:
                self.ensure_initialized()
            except Exception:
                raise _exc("BlockedError")(
                    "H2S GraphQL 返回 403，CF 会话重建失败。"
                    "可能需要更换 IP 或等待冷却。"
                )
            retry = self._page.evaluate(js_code)
            if "error" in retry:
                raise _exc("ScrapeNetworkError")(f"重建后重试失败: {retry['error']}")
            if retry["status"] == 403:
                logger.warning(
                    "GraphQL 重建会话后仍返回 403 headers=%s body=%s",
                    retry.get("headers", {}),
                    retry.get("text", "")[:300],
                )
                raise _exc("BlockedError")(
                    "H2S GraphQL 持续返回 403。可能需要更换 IP 或等待冷却。"
                )
            result = retry
            status = result["status"]

        if status == 429:
            raise _exc("RateLimitError")("H2S GraphQL 返回 429 Too Many Requests")

        if not result["ok"] and status >= 400:
            raise _exc("ScrapeNetworkError")(
                f"H2S GraphQL HTTP {status}: {result['text'][:300]}"
            )

        import json

        try:
            return json.loads(result["text"])
        except json.JSONDecodeError as e:
            raise _exc("ScrapeNetworkError")(
                f"H2S GraphQL 响应非 JSON: {e}"
            ) from e
