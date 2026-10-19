"""抓取调度闸门：分片与「代理全挂时只抓直连得通的 source」。

从 monitor.py 拆出。``apply_source_intervals``（按 source 节流）暂时留在
monitor.py——它依赖进程内的 ``_source_pacing`` 单例，等 RuntimeState 那一步
再一起搬。日志通道仍用 ``monitor``。
"""
from __future__ import annotations

import logging

from storage import Storage
from mcore.sources import PROXYLESS_CAPABLE_SOURCES

logger = logging.getLogger("monitor")

SHARD_CURSOR_PREFIX = "shard_cursor:"


def shard_source_tasks(
        tasks: list,
        source: str,
        size: int,
        cursor: int,
) -> tuple[list, int]:
    """取某个 source 本轮该抓的切片，返回 ``(切片, 下一轮游标)``。

    纯函数，游标读写留给调用方——这样分片规则本身能脱离 DB 测试。

    ``size <= 0`` 或 ``size >= len(tasks)`` 时不分片（原样返回）：配置成
    「每轮 5 个」而实际只有 4 个 target 时，行为必须与没配过一样。

    切片会绕回开头，所以 target 数不是 size 整数倍时也能均匀覆盖，不会让
    末尾那几个永远排在同一轮。
    """
    n = len(tasks)
    if size <= 0 or n == 0 or size >= n:
        return tasks, 0
    start = cursor % n
    idx = [(start + i) % n for i in range(size)]
    return [tasks[i] for i in idx], (start + size) % n





def apply_proxyless_gate(tasks: list) -> list:
    """代理全挂时，把直连打不通的 source 整体摘出本轮。

    为什么要摘
    ----------
    2026-08-26 生产实况：webshare 两个账号同时 402，五次「降级直连原生 IP」，
    **一轮都没抓成**——H2S / Xior 在直连下必然失败，而
    ``dispatch_scrape_tasks`` 的判据是「全员失败就上抛 ProxyError」，于是那两家
    的必然失败把同一轮里本来能成的 OurDomain / OurCampus 一起掀掉了。

    摘掉之后这一轮就只剩打得通的那些，成功数不再是 0，整轮也就不会被上抛的
    ProxyError 中止。代价是 H2S / Xior 在代理恢复前完全不抓——但它们本来也
    抓不到，区别只是「安静地等」还是「每轮制造一批注定失败的请求」。

    这也让遥测说了实话：被摘掉的 source 本轮不写 round_stats，健康判定看到的
    是「没有这一轮」，而不是「这一轮失败了」。把等代理算成抓取失败，会让
    fail_streak 报出一个和成因无关的结论——判据和被判的东西不是一回事。
    """
    from config import is_proxy_native_fallback_active

    try:
        if not is_proxy_native_fallback_active():
            return tasks
    except Exception:
        # 判不出代理状态就当作正常：宁可让它去试一轮，也不能因为读状态出错
        # 把 source 静默停掉。
        logger.debug("代理降级状态判定失败，本轮不启用直连闸门", exc_info=True)
        return tasks

    kept, dropped = [], []
    for t in tasks:
        (kept if (t.source or "") in PROXYLESS_CAPABLE_SOURCES else dropped).append(t)

    if dropped:
        by_source: dict[str, int] = {}
        for t in dropped:
            by_source[t.source] = by_source.get(t.source, 0) + 1
        logger.warning(
            "🛰️ 代理全部失效，本轮跳过直连打不通的 source：%s"
            "（它们在机房 IP 上会被 Cloudflare 挡住，不是抓取故障）｜"
            "仍在抓：%s",
            "、".join(f"{k}×{v}" for k, v in sorted(by_source.items())),
            "、".join(sorted({t.source for t in kept})) or "无",
        )
    return kept


def apply_task_sharding(
        tasks: list,
        cfg,
        storage: Storage,
        *,
        dry_run: bool = False,
) -> list:
    """按 ``cfg.shard_sizes`` 把 target 多的 source 拆到多轮抓。

    为什么必须有这个：Xior 的请求间隔是 5s（限流按速率算，调小会直接撞回
    429），实测每栋楼 13.9 秒。官方注册表 30 栋 ≈ 417 秒/轮，而 CHECK_INTERVAL
    是 300 秒；更糟的是 H2S 排在其它 source **之后**执行，不分片等于每轮把真正
    出房源的那个 source 推迟 7 分钟。

    游标存 meta，重启后接着转——否则每次重启都从第一片开始，后面的楼栋会被
    系统性地少抓。

    任何异常都回退成「不分片」：宁可这一轮慢，也不能悄悄漏抓楼栋。
    """
    sizes = getattr(cfg, "shard_sizes", None) or {}
    if not sizes:
        return tasks

    by_source: dict[str, list] = {}
    for t in tasks:
        by_source.setdefault(t.source, []).append(t)

    out: list = []
    for src, group in by_source.items():
        size = int(sizes.get(src, 0) or 0)
        if size <= 0 or size >= len(group):
            out.extend(group)
            continue
        key = SHARD_CURSOR_PREFIX + src
        try:
            cursor = int(storage.get_meta(key, default="") or 0)
        except (TypeError, ValueError):
            cursor = 0
        except Exception:
            logger.debug("读取 %s 分片游标失败，本轮不分片", src, exc_info=True)
            out.extend(group)
            continue

        picked, next_cursor = shard_source_tasks(group, src, size, cursor)
        if not dry_run:
            try:
                storage.set_meta(key, str(next_cursor))
            except Exception:
                # 游标写不进去就不分片：否则每轮都从同一个位置切，后面的楼栋永远抓不到
                logger.warning("写 %s 分片游标失败，本轮改为全量抓取", src, exc_info=True)
                out.extend(group)
                continue
        logger.info(
            "source %s 分轮抓取：本轮 %d/%d 个 target（%s），下轮游标 %d",
            src, len(picked), len(group),
            ", ".join(t.city_display for t in picked), next_cursor,
        )
        out.extend(picked)
    return out
