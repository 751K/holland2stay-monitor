"""抓取阶段的失败处置与 H2S 熔断后的通知。

从 monitor.run_once 的 except 分支提出来。控制流保持原样：多数分支在告警/冷却
之后重抛（让 main_loop 做退避），只有未分类异常返回 ``{}`` 走正常间隔。
日志通道仍用 ``monitor``。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from config import (
    get_proxy_url,
    is_proxy_in_cooldown,
    is_proxy_native_fallback_active,
    proxy_failure_mark_count,
    report_proxy_failure,
)
from mcore.h2s_guard import (
    H2S_LONG_BLOCK_STREAK,
    H2S_SOURCE,
    h2s_circuit_remaining,
)
from mcore.outage import notify_admin_only
from mcore.state import runtime
from mcore.throttles import (
    should_notify_block,
    should_notify_h2s_long_block,
    should_notify_internal,
    should_notify_maintenance,
    should_notify_proxy,
)
from scrapers import (
    BlockedError,
    OperationNotAllowedError,
    ProxyError,
    RateLimitError,
    ScrapeNetworkError,
    UpstreamMaintenanceError,
    is_proxy_account_error,
    is_proxy_service_error,
)

logger = logging.getLogger("monitor")


def _task_labels(tasks) -> list[str]:
    return [f"{t.source}:{t.city_display}" for t in tasks]


async def handle_h2s_blocked(
    h2s_blocked,
    h2s_mode: str,
    fresh: list,
    completeness: dict[str, bool],
    *,
    storage,
    web_notifier,
    dry_run: bool,
) -> dict[str, bool] | None:
    """H2S 403 后的 admin 告警；返回 completeness 表示本轮到此为止。"""
    if h2s_blocked is not None:
        proxy_on = bool(get_proxy_url())
        if not dry_run and should_notify_block():
            cooldown = h2s_circuit_remaining()
            err_msg = (
                f"🚫 H2S 抓取被 403 屏蔽\n\n{h2s_blocked}\n\n"
                f"代理状态: {'已启用' if proxy_on else '未启用'}\n"
                f"H2S 已进入 source 熔断，约 {cooldown // 60} 分钟后只做一次 canary 探测；"
                f"其他平台会继续监控。30 分钟内不会重复通知。"
            )
            await notify_admin_only(
                storage, web_notifier, err_msg, kind="h2s_circuit",
            )
        if (
            not dry_run
            and runtime.source_circuits.fail_streak(H2S_SOURCE) >= H2S_LONG_BLOCK_STREAK
            and should_notify_h2s_long_block()
        ):
            cooldown = h2s_circuit_remaining()
            await notify_admin_only(
                storage,
                web_notifier,
                (
                    "H2S 长时间被 block，需要检查服务器\n\n"
                    f"连续 H2S 403: {runtime.source_circuits.fail_streak(H2S_SOURCE)} 次\n"
                    f"当前 H2S 熔断剩余: 约 {max(1, cooldown // 60)} 分钟\n"
                    f"最近错误: {h2s_blocked}\n\n"
                    "请检查服务器网络、代理出口、Webshare 状态和 H2S GraphQL 访问情况。"
                ),
                kind="blocked",
            )
        if not fresh:
            logger.warning(
                "H2S %s 触发 403 且没有其它 source 成功，本轮不更新数据库。",
                h2s_mode,
            )
            return completeness
        return None

    if h2s_mode == "open" and not fresh:
        # 条件是 `not fresh`（本轮一条房源都没抓到），不是「没有配置其它 source
        # 的任务」——原文案写成后者，会把人引去查 SOURCES 配置，而真实原因通常
        # 是其它 source 确实各自返回了 0 条。
        logger.warning(
            "H2S 熔断中，且其它 source 本轮未抓到任何房源，无数据可入库。"
        )
        return completeness
    return None


async def handle_scrape_failure(
    exc: BaseException,
    *,
    storage,
    web_notifier,
    scrape_tasks,
    user_notifiers,
    dry_run: bool,
    prewarm,
) -> dict[str, bool]:
    """处置抓取阶段异常。

    多数分支在告警/冷却之后 ``raise``（重抛当前异常，交给 main_loop 退避）；
    只有未分类异常返回 ``{}``，走正常间隔。
    """
    await prewarm.stash_pending()

    if isinstance(exc, OperationNotAllowedError):
        # 403，但正文是上游应用说「这条 operation 没登记」，与出口 IP 无关。
        # 不换 IP、不熔断、不抑制登录：那三件事对这种 403 一件都不管用。
        logger.error(
            "⛔ 上游拒绝了我们发的 GraphQL operation（HTTP 403，非 Cloudflare）"
            " cities=%d users=%d：%s",
            len(scrape_tasks), len(user_notifiers), exc,
        )
        if not dry_run and should_notify_internal():
            await notify_admin_only(
                storage, web_notifier,
                f"⛔ 上游按 operation 白名单拒绝\n\n{exc}\n\n"
                f"这不是 Cloudflare 屏蔽，也不是网络问题——换 IP、重建会话、"
                f"等冷却都无效。\n"
                f"唯一的修法是把站点自己发的那条 operation **原样照抄**回来"
                f"（步骤见 docs/H2S.md §5.1：钩住 crypto.subtle.encrypt 截获"
                f"加密前的明文）。\n"
                f"注意白名单按 operationName + 归一化字段集比对，删一个字段"
                f"就是 403。\n"
                f"30 分钟内不重复通知。",
                kind="error",
            )
        raise exc

    if isinstance(exc, BlockedError):
        # 403 = Cloudflare WAF 屏蔽，等待无法恢复，必须换代理/重启。
        proxy_on = bool(get_proxy_url())
        logger.error(
            "🚫 抓取被屏蔽 (HTTP 403) cities=%d users=%d proxy=%s: %s",
            len(scrape_tasks), len(user_notifiers),
            "yes" if proxy_on else "no", exc,
        )
        if not dry_run and should_notify_block():
            err_msg = (
                f"🚫 抓取被 403 屏蔽\n\n{exc}\n\n"
                f"代理状态: {'已启用' if proxy_on else '未启用'}\n"
                f"30 分钟内不会重复通知。"
            )
            await notify_admin_only(
                storage, web_notifier, err_msg, kind="scrape_blocked",
            )
        raise exc

    if isinstance(exc, RateLimitError):
        logger.warning(
            "⚠️  抓取被限流 cities=%d users=%d proxy=%s: %s",
            len(scrape_tasks), len(user_notifiers),
            "yes" if get_proxy_url() else "no",
            exc,
        )
        if not dry_run:
            err_msg = f"⚠️ 抓取被限流（429）\n{exc}\n监控将暂停 5 分钟后继续。"
            await notify_admin_only(
                storage, web_notifier, err_msg, kind="scrape_rate_limited",
            )
        raise exc

    if isinstance(exc, UpstreamMaintenanceError):
        # 平台维护：自己会恢复，不给普通用户发告警；给 admin 面板发一条。
        logger.info(
            "🔧 H2S 平台维护中，本轮跳过 cities=%d users=%d: %s",
            len(scrape_tasks), len(user_notifiers), exc,
        )
        if not dry_run:
            now_iso = datetime.now(timezone.utc).isoformat()
            first_detect = not storage.get_meta("upstream_maintenance_seen_at", default="")
            if first_detect:
                storage.set_meta("upstream_maintenance_seen_at", now_iso)
            storage.set_meta("upstream_maintenance_last_at", now_iso)

            if web_notifier and should_notify_maintenance():
                hint = (
                    "🔧 H2S 平台计划维护中\n\n"
                    f"{exc}\n\n"
                    "监控已暂停轮询，平台恢复后会自动继续。"
                    "无需操作；状态会在 dashboard 顶部 banner 显示。"
                )
                await web_notifier.send_error(hint)
        raise exc

    if isinstance(exc, ProxyError):
        # 抓取代理失效。ProxyError 是 ScrapeNetworkError 子类——控制流仍走
        # 网络失败冷却，但要把失败的代理标记进 cooldown 并告警 admin。
        old_proxy = get_proxy_url()
        service_error = is_proxy_service_error(exc)
        account_level = is_proxy_account_error(exc)
        new_proxy = report_proxy_failure(
            service_error_confirmed=service_error, account_level=account_level,
        )
        confirmed_down = is_proxy_in_cooldown(old_proxy)
        native_fallback = is_proxy_native_fallback_active()
        switched_proxy = bool(new_proxy and new_proxy != old_proxy)
        logger.error(
            "🛰️ 抓取代理故障%s: %s",
            (
                "，下一轮切换到备用代理"
                if switched_proxy
                else "，下一轮降级为服务器原生 IP 直连"
                if native_fallback
                else f"，服务端异常待确认（{proxy_failure_mark_count(old_proxy)}/2）"
                if old_proxy and not confirmed_down and service_error
                else "，未确认代理服务端异常，按普通网络失败处理"
                if old_proxy and not service_error
                else "（无备用代理可切）"
            ),
            exc,
        )
        should_alert = switched_proxy or native_fallback or not old_proxy
        if not dry_run and should_alert and should_notify_proxy():
            if switched_proxy:
                tail = "已自动切换到备用代理，下一轮重试。请尽快修复主代理。"
            elif native_fallback:
                tail = (
                    "所有已配置代理都在冷却中，监控将临时降级为服务器原生 IP 直连。"
                    "降级期间抓取频率最多 10 分钟一次，代理冷却结束后会自动恢复。"
                )
            else:
                tail = (
                    "监控已暂停抓取（代理不通时无法绕过 Cloudflare）。"
                    "请检查 HTTPS_PROXY，或配置 SCRAPE_PROXIES_FALLBACK 备用代理。"
                )
            msg = f"抓取代理失效\n\n{exc}\n\n{tail}\n30 分钟内不重复通知。"
            if web_notifier:
                await web_notifier.send_error(msg)
            from mcore import push as _push
            try:
                await _push.dispatch_admin(storage, msg, kind="proxy")
            except Exception:
                logger.debug("代理失效 admin push 失败（已忽略）", exc_info=True)
        raise exc

    if isinstance(exc, ScrapeNetworkError):
        logger.error(
            "抓取全部网络失败 cities=%s users=%d proxy=%s: %s",
            _task_labels(scrape_tasks), len(user_notifiers),
            "yes" if get_proxy_url() else "no",
            exc,
        )
        raise exc

    # 未分类异常：只告警 admin 并加 30 min 节流，然后走正常间隔。
    logger.error(
        "抓取阶段未分类错误 cities=%s users=%d: %s",
        _task_labels(scrape_tasks), len(user_notifiers), exc,
        exc_info=True,
    )
    if not dry_run and should_notify_internal():
        await notify_admin_only(
            storage, web_notifier,
            f"抓取阶段未分类错误\n\n{type(exc).__name__}: {exc}\n\n"
            f"这是一条未被归类的内部异常，请查看服务器日志排查。"
            f"30 分钟内不重复通知。",
            kind="error",
        )
    return {}
