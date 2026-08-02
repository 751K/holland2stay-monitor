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

# CF 挑战页判据。几个看起来能用但实测不能用的候选：
#   - ``challenges.cloudflare.com`` / ``/cdn-cgi/challenge-platform/``：
#     挑战解开后的真实页面里同样存在（CSP 头 + 站点自带 turnstile），
#     用它们判定会永远认为挑战没过。
#   - URL 里的 ``__cf_chl_rt_tk``：CF 靠 ``history.replaceState`` 回写 URL，
#     时机不定；实测挑战已解开、真实 DOM 已就位时 URL 仍可能带着它，
#     用它判定会把正常会话误判成被挡。
# 只有挑战页脚本自身的 ``_cf_chl_opt`` 会随文档被真实页面替换而消失。
_CF_CHALLENGE_HTML_MARKER = "_cf_chl_opt"

# 挑战解开的等待上限。实测差异很大：macOS 本地约 3s，1 CPU 的生产 VPS 上
# headless Chromium 跑完 challenge 要 30s 量级。上限按最慢的环境留足余量，
# 超时说明这个 IP 当前过不去，交给上层熔断而不是硬发请求。
_CHALLENGE_CLEAR_TIMEOUT = 90.0
_CHALLENGE_POLL_INTERVAL = 1.0

# clearance 未生效时 H2S 返回的标记（403 + 这段 JSON）
_CLEARANCE_REQUIRED_MARKER = "clearance_required"
# 探测用的最小查询：只取 total_count，不翻页不取字段
_CLEARANCE_PROBE_QUERY = (
    '{products(filter:{category_uid:{eq:"Nw=="}},pageSize:1){total_count}}'
)
_CLEARANCE_TIMEOUT = 60.0
_CLEARANCE_POLL_INTERVAL = 2.0
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

        # goto(domcontentloaded) 返回时页面通常还是 CF 挑战页，必须先等挑战
        # 真正解开，再做任何基于页面内容的判定——否则读到的标题和正文是
        # Cloudflare 的，不是 H2S 的。
        self._wait_for_challenge_clear()
        challenge_elapsed = time.monotonic() - start

        # 挑战已解开，此刻的标题/正文才代表站点真实状态
        self._raise_if_maintenance_page()

        # 最后一关：确认 clearance 真的生效。
        # 这里以前等的是 ``[data-cy="FilterList-item"]``——实测它和能否发请求
        # 无关（GraphQL 已经 200 时该元素仍可能没渲染），而且等不到还只告警
        # 就继续，等于把「没通过校验」当成「通过了」。改为直接探 GraphQL：
        # 能拿到响应才算初始化完成。
        self._wait_for_clearance()

        # 两段耗时分开记：挑战慢通常是机器/网络慢，clearance 慢更像 CF 在
        # 加码校验，排查时是两个方向。
        elapsed = time.monotonic() - start
        logger.info(
            "CF 挑战完成，clearance 已生效 (共 %.1fs：挑战 %.1fs + clearance %.1fs)",
            elapsed, challenge_elapsed, elapsed - challenge_elapsed,
        )
        self._initialized = True

    def _is_challenge_page(self) -> bool:
        """当前页面是否仍是 CF 挑战页。"""
        try:
            return _CF_CHALLENGE_HTML_MARKER in self._page.content()
        except Exception:
            # 页面内容取不到（导航中 / 崩溃）时保守认为仍在挑战，
            # 由 _wait_for_challenge_clear 的超时统一处理。
            return True

    def _wait_for_challenge_clear(
        self, timeout: float = _CHALLENGE_CLEAR_TIMEOUT
    ) -> None:
        """轮询到 CF 挑战解开为止；超时抛 BlockedError。

        这是「挑战是否通过」的唯一判据。以前用 ``[data-cy=FilterList-item]``
        选择器代替，超时后仅告警并继续，导致挑战没过也照发 GraphQL——
        必然 403，再触发会话重建，最终把 source 打进熔断。
        """
        deadline = time.monotonic() + timeout
        while True:
            if not self._is_challenge_page():
                return
            if time.monotonic() >= deadline:
                raise _exc("BlockedError")(
                    f"CF 挑战 {timeout:.0f}s 内未解开，H2S 主站仍停在挑战页。"
                    "可能需要更换 IP 或等待冷却。"
                )
            time.sleep(_CHALLENGE_POLL_INTERVAL)

    def _raise_if_maintenance_page(self) -> None:
        """浏览器已打开主站时，识别 H2S 维护页并走维护冷却分支。

        必须在 CF 挑战解开后调用：挑战页是 Cloudflare 生成的，其标题和正文
        与 H2S 的真实状态无关，在挑战解开前判定等于拿 CF 的页面去猜 H2S 在
        不在维护。
        """
        if self._is_challenge_page():
            return

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
    def _raw_fetch_gql(
        self,
        query: str,
        variables: dict | None = None,
        *,
        timeout_ms: int = 30_000,
        extra_headers: dict[str, str] | None = None,
    ) -> dict:
        """在页面里发一次 GraphQL fetch，原样返回 ``{status, ok, text, headers}``。

        不做任何状态码处理，也**不会**触发 ``ensure_initialized``——
        clearance 探测要在初始化过程中调用它，走公开的 fetch_gql 会无限递归。
        """
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
        return self._page.evaluate(js_code)

    @staticmethod
    def _is_clearance_required(result: dict) -> bool:
        """403 是否属于「clearance 还没落地」这种**瞬时**状态。

        H2S 在这种情况下回一个明确的 JSON：
        ``{"error":"Browser verification required","code":"clearance_required"}``
        它和「这个 IP 被 CF 挡了」是两回事——前者再等一两秒就好，后者
        换 IP 才有用。混为一谈会把马上要生效的会话反复推倒重建。
        """
        if result.get("status") != 403:
            return False
        return _CLEARANCE_REQUIRED_MARKER in (result.get("text") or "")

    def _wait_for_clearance(self, timeout: float = _CLEARANCE_TIMEOUT) -> None:
        """轮询到 GraphQL 不再回 clearance_required 为止。

        挑战页消失只说明文档被真实页面替换了，``cf_clearance`` cookie 未必
        已经生效——实测两者之间有约 2s 的空窗，这期间发请求必然 403。
        真正的「初始化完成」是这个探测通过。
        """
        deadline = time.monotonic() + timeout
        while True:
            try:
                result = self._raw_fetch_gql(_CLEARANCE_PROBE_QUERY, timeout_ms=15_000)
            except Exception as e:
                # 页面还在动（导航 / 重绘）时 evaluate 可能直接抛，等下一轮
                logger.debug("clearance 探测异常，重试: %s", e)
                result = {"error": str(e)}

            if "error" not in result and not self._is_clearance_required(result):
                return
            if time.monotonic() >= deadline:
                raise _exc("BlockedError")(
                    f"CF clearance {timeout:.0f}s 内未生效，GraphQL 持续要求浏览器校验。"
                    "可能需要更换 IP 或等待冷却。"
                )
            time.sleep(_CLEARANCE_POLL_INTERVAL)

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
        result = self._raw_fetch_gql(
            query, variables, timeout_ms=timeout_ms, extra_headers=extra_headers
        )
        if "error" in result:
            raise _exc("ScrapeNetworkError")(f"浏览器内 fetch 失败: {result['error']}")

        status = result["status"]

        # clearance 过期是长会话里的常态（token 有寿命），不是被封。
        # 先原地等它重新生效再重试，等不到才按屏蔽处理——直接推倒重建
        # 浏览器代价高，且重建过程本身也要重新过挑战，容易连锁失败。
        if self._is_clearance_required(result):
            logger.info("GraphQL 要求重新校验，等待 clearance 生效后重试...")
            self._wait_for_clearance()
            result = self._raw_fetch_gql(
                query, variables, timeout_ms=timeout_ms, extra_headers=extra_headers
            )
            if "error" in result:
                raise _exc("ScrapeNetworkError")(
                    f"clearance 恢复后重试失败: {result['error']}"
                )
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
            except _exc("UpstreamMaintenanceError"):
                # 重建时发现平台在维护：这是维护，不是屏蔽。压成 BlockedError
                # 会让 monitor 走熔断 + admin 告警，而不是安静的维护冷却。
                raise
            except Exception as e:
                raise _exc("BlockedError")(
                    "H2S GraphQL 返回 403，CF 会话重建失败。"
                    "可能需要更换 IP 或等待冷却。"
                ) from e
            retry = self._raw_fetch_gql(
                query, variables, timeout_ms=timeout_ms, extra_headers=extra_headers
            )
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
