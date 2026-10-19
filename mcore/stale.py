"""stale listing 收敛：老化窗口与「已移出监控的城市」。

从 monitor.py 拆出。日志通道仍用 ``monitor``——面板、告警和测试都按这个
logger 名字过滤，改名属于另一个变更。
"""
from __future__ import annotations

import logging
import os

from storage import Storage

logger = logging.getLogger("monitor")


def monitored_pairs(cfg) -> list[tuple[str, str]]:
    """当前配置里全部 (source, city) 抓取目标。

    给 ``mark_stale_listings`` 判断"哪些城市已经彻底不监控了"用。读配置失败
    时返回空列表 → 孤儿收敛整条跳过（fail-open）。绝不能因为一次配置读取
    异常就把整库判成孤儿。
    """
    try:
        return [(t.source, t.city_display) for t in cfg.scrape_tasks_v2()]
    except Exception:
        logger.debug("读取监控目标失败，跳过孤儿收敛", exc_info=True)
        return []


#: 「消失多久算不再可订 / 算彻底没了」。四个平台、所有状态统一一套。
#:
#: 曾经按 (source, 状态类) 分开配过，最后收掉了：那些差别描述的是「feed 会不会
#: 保留下架房源」，而实测下来四个平台的终态都是**从 feed 里消失**——只有 Xior
#: 的 feed 里真有 Occupied，其余三个平台的终态基本全靠推。既然消失是共同的
#: 下架信号，就不该有四套判据。
#:
#: 数怎么定的：轮次约 1 分钟一次，30 分钟 ≈ 30 轮连续完整扫描里都没有它，
#: 而同一次响应里通常还有二十几条别的房源作旁证。OurDomain / OurCampus 另有
#: 一道闸——``scrapers.ourdomain`` 要求连续 3 轮返回 0 个单元才承认「真没房」。
#:
#: 收得太早的代价不是「少通知」而是「多通知」：房源被误判之后又出现在 feed 里，
#: 会产生一次状态变更，用户收到一条假的重新上架。中间那站 Reserved 就是为此
#: 存在的——2 小时内回来只是 ``Reserved → 可订``，是最常见的正常迁移。
STALE_RESERVED_HOURS = 0.5
#: 2 小时对齐 **H2S 官方的付款限时**：消失超过它，预留必然已经落定。
STALE_OCCUPIED_HOURS = 2.0


def stale_hours() -> tuple[float, float]:
    """``(消失多久转 Reserved, 消失多久判 Occupied)``，环境变量可覆盖。"""
    def _read(name: str, default: float) -> float:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        try:
            return max(0.25, min(24.0 * 30, float(raw)))
        except ValueError:
            logger.warning("%s=%r 不是数字，改用默认值 %s", name, raw, default)
            return default

    reserved = _read("STALE_RESERVED_HOURS", STALE_RESERVED_HOURS)
    occupied = _read("STALE_OCCUPIED_HOURS", STALE_OCCUPIED_HOURS)
    # 终态窗口必须 >= 中间站，否则第二段会抢在第一段之前把房源直接判死，
    # 中间那一站形同虚设——而它正是「判错时代价小」的全部来源。
    if occupied < reserved:
        logger.warning(
            "STALE_OCCUPIED_HOURS(%s) 小于 STALE_RESERVED_HOURS(%s)，"
            "会让 Reserved 那一站失效；已按 reserved 取齐。", occupied, reserved,
        )
        occupied = reserved
    return reserved, occupied


def mark_stale_listings_for_complete_cities(
        storage: Storage,
        completeness: dict[str, bool],
        *,
        monitored_pairs: list[tuple[str, str]] | None = None,
        orphan_days: int = 30,
) -> int:
    """只对本轮完整扫描成功的城市执行 stale listing 收敛。

    key 带 ``source:`` 前缀时按 (source, city) 精确限定，避免用一个 source
    的完整性去收敛另一个 source 的同名城市。

    ``monitored_pairs`` 是**配置里**的全部目标，和上面那个"本轮扫全了的"
    不是一回事——分片和节流会让正常监控的城市这轮不出现。它只用于孤儿路径：
    把已经彻底移出监控的城市里的鬼影 listing 也收敛掉。
    """
    complete_cities: list[str] = []
    complete_source_cities: list[tuple[str, str]] = []
    for key, ok in completeness.items():
        if not ok:
            continue
        if ":" in key:
            source, city = key.split(":", 1)
            complete_source_cities.append((source, city))
        else:
            complete_cities.append(key)

    if not complete_cities and not complete_source_cities:
        logger.info("跳过 stale listing 状态收敛：本轮无完整扫描城市")
        return 0

    reserved_hours, occupied_hours = stale_hours()
    return storage.mark_stale_listings(
        cities=complete_cities if complete_cities else None,
        source_city_pairs=complete_source_cities if complete_source_cities else None,
        monitored_pairs=monitored_pairs,
        orphan_days=orphan_days,
        reserved_hours=reserved_hours,
        occupied_hours=occupied_hours,
        full_lifecycle_sources=full_lifecycle_sources(),
    )


def full_lifecycle_sources() -> set[str]:
    """feed 已覆盖「已预留」状态的 source。读配置失败时返回空集。

    fail-open 到旧行为：拿不准就按「消失有歧义」处理，先推 Reserved。宁可多一站
    推测，也不要因为一次配置读取失败就把还在付款窗口里的房源直接判终态。
    """
    try:
        from config import load_config
        return set(load_config().sources_with_full_lifecycle())
    except Exception:
        logger.warning("读取 full_lifecycle_sources 失败，按旧判据收敛", exc_info=True)
        return set()


def sweep_aging(storage: Storage, completeness: dict[str, bool]) -> int:
    """每轮跑一趟老化收敛（不含孤儿路径），返回收敛条数。

    为什么必须每轮跑
    ----------------
    孤儿那趟 24 小时一次。那个节奏对 30 天的宽限期是合适的，但会把小时级的
    老化窗口整个吃掉——房源满 2 小时该判终态了，实际要等到下一次 24 小时的
    整点，最坏挂 26 小时。**阈值调了不改节奏，等于没调。**

    每轮跑没有累积开销：到终态的行之后一律被 WHERE 排除，稳态下命中 0 行。
    **阈值本身就是节流器**，不需要再加一个间隔开关。

    抽成函数不是为了复用（只有一个调用点），是为了让**调用点本身**可测：
    这里最容易写错的是「顺手把 ``monitored_pairs`` 也传进去」，那会把扫全库
    的孤儿收敛拉进一条每轮都跑的路径上。那种错误在功能上看不出来——孤儿本来
    也该被收敛，只是不该由这一趟来做——只有盯着调用参数才能发现。
    """
    return mark_stale_listings_for_complete_cities(storage, completeness)


def stale_sweep_decision(
    completeness: dict[str, bool],
    last_sweep_at: float,
    interval_sec: float,
    *,
    now: float,
) -> str:
    """本轮该不该跑 stale 收敛。返回 ``"run"`` / ``"wait"`` / ``"defer"``。

    ``defer`` = 到点了、但本轮一个完整扫描城市都没有。这时**不能重置计时器**：
    run_once 的兜底路径（未分类错误 / 管线错误）和 H2S 熔断期都会返回空
    completeness，24 小时那一次刚好撞上就会白白跳过，鬼影 listing 再多挂一天。
    """
    if now - last_sweep_at < interval_sec:
        return "wait"
    if not any(completeness.values()):
        return "defer"
    return "run"
