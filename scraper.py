"""
scraper.py — 向后兼容 re-export
=================================

H2S GraphQL 抓取逻辑已迁移至 ``scrapers/holland2stay.py``（CloakBrowser +
www.holland2stay.com/api/graphql）。

旧 curl_cffi 直连路径已退役，本文件仅保留从 ``scrapers.base`` 的 re-export，
维持旧 import 路径兼容（monitor.py / tests 等）。

所有异常类（BlockedError / RateLimitError / ProxyError / ScrapeNetworkError /
UpstreamMaintenanceError）定义在 ``scrapers/base.py``，经 ``scrapers.__init__``
导出。本文件的 re-export 仅用于尚未迁移的历史代码。
"""
from __future__ import annotations

from scrapers.base import (  # noqa: F401  (re-export for backwards compat)
    RATE_LIMIT_BACKOFF,
    BlockedError,
    ProxyError,
    RateLimitError,
    ScrapeNetworkError,
    UpstreamMaintenanceError,
    is_cloudflare_body,
    is_maintenance_body,
    is_proxy_error,
    is_proxy_service_error,
    probe_h2s_maintenance,
)
