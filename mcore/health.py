"""mcore/health.py — 分 source 的数据健康判定
================================================

把 ``round_stats`` 的原始行聚合成「这个 source 现在还正常吗」。

和 ``/health`` 的分工
--------------------
``/health`` 回答**「循环还活着吗」**（monitor 心跳新鲜度），这里回答
**「数据还对吗」**。两者必须分开：

- 心跳正常但解析器被上游改版打坏 → ``/health`` 绿，数据全错
- H2S 熔断冷却最长 6 小时 → 数据停更，但这是**按设计退避**，不是故障

所以本模块的产出只用于告警和面板，**不参与 ``/health`` 的状态码**。
容器重启治不好解析器对不上，只会打断正在进行的抓取。

放在 ``mcore/`` 而不是 ``app/services/`` 的原因：monitor 进程（无 Flask 应用
上下文）和 Web 面板都要用它，``app/`` 下的东西 monitor 不该碰。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# ── 阈值 ────────────────────────────────────────────────────────────
#
# 默认值按 5 分钟轮次估：3 轮 ≈ 15 分钟。够长到不被一次网络抖动触发，
# 够短到在用户反馈之前先知道。

def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return default


# 连续失败几轮算 down
FAIL_STREAK_DOWN = _env_int("HEALTH_FAIL_STREAK_DOWN", 3)
# 连续抓到 0 条几轮算 warn（还要满足窗口内曾经非零，见 _judge）
ZERO_STREAK_WARN = _env_int("HEALTH_ZERO_STREAK_WARN", 3)
# 距上次完整扫描超过多少秒算 warn
#
# 这条规则取代了原先的「完整扫描率低于 80%」。**分层抓取让那个阈值永久为真**：
# H2S 每轮只查 _FRESH_STATUSES，一律 complete=False，只有每 30 分钟一次的全量
# 轮才可能为 True，于是完整率结构性地停在 10% 上下。它每天都在告警，唯一的效果
# 是训练人忽略告警。
#
# 换成「距上次完整扫描多久」之后，一条规则同时适用于两类 source，因为它量的
# 是**后果**而不是比率——stale 收敛只在完整轮里发生，多久没有完整轮，下架判定
# 就滞后多久：
#
#   H2S（每 30 分钟一次全量）   连着漏掉 3 次全量才报
#   其余（每轮都是全量）        连续 90 分钟没有任何完整轮才报
#
# 默认 5400 秒 = H2S 全量间隔的 3 倍。实测其间隔中位 31.2 分、最大 36.9 分，
# 留足余量。
STALE_FULL_SCAN_SECONDS = _env_int("HEALTH_STALE_FULL_SCAN_SECONDS", 5400)

#: 熔断跳过的那一轮在遥测里的 error_type 标记。它**不是抓取失败**——是按设计
#: 退避，见 _judge 里 fail_streak 的处理。
CIRCUIT_OPEN_ERROR = "CircuitOpen"

# 判定窗口：每个 source 回看多少轮
DEFAULT_WINDOW = _env_int("HEALTH_WINDOW_ROUNDS", 24)

def fmt_ts(iso: str, *, fallback: str = "") -> str:
    """UTC ISO → ``TIMEZONE`` 本地时间的可读串，给告警文案用。

    库里存 UTC，但人读的地方必须是本地时间——容器跑在 ``TZ=Europe/Amsterdam``，
    日志的 asctime 就是那个时区。告警里塞一个 UTC ISO，收到推送的人还得自己
    换算才能去日志里对上那一刻。

    这里直接读环境变量而不是 import config：``mcore`` 至今没有依赖 ``config``，
    为了一个时区名破坏这个边界不值当。默认值与 ``config.TIMEZONE`` 保持一致。
    """
    if not iso:
        return fallback
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(os.environ.get("TIMEZONE") or "Europe/Amsterdam")
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(tz).strftime("%m-%d %H:%M")
    except Exception:
        return str(iso)


STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_DOWN = "down"
STATUS_UNKNOWN = "unknown"


@dataclass(slots=True)
class SourceHealth:
    """一个 source 在判定窗口内的健康快照。"""

    source: str
    status: str = STATUS_UNKNOWN
    reasons: list[str] = field(default_factory=list)

    rounds: int = 0                 # 窗口内实际有几行遥测
    last_round_at: str = ""
    last_success_at: str = ""       # 最近一次 error_type 为空的轮次
    last_nonzero_at: str = ""       # 最近一次抓到 >0 条的轮次
    fail_streak: int = 0            # 从最新往回数，连续失败几轮
    zero_streak: int = 0            # 从最新往回数，连续 0 条几轮（失败轮打断计数）
    completeness_rate: float = -1.0  # sum(complete)/sum(targets)；无数据为 -1
    # 最近一次「至少有一个 target 抓全了」的轮次。**跨窗口**取自库里，不能只看
    # 窗口内——H2S 的窗口（24 轮 ≈ 36 分钟）比它的全量间隔（30 分钟）还短，
    # 窗口里常常一次全量都不含，据此判定必然误报。
    last_complete_at: str = ""
    # 距上次完整扫描多少秒；算不出（无数据）为 -1
    stale_full_scan_seconds: float = -1.0
    # 窗口内有几轮是熔断跳过的。它们既不算失败也不算成功，只用于文案。
    circuit_open_rounds: int = 0
    avg_listings: float = 0.0
    max_listings: int = 0
    last_listings: int = 0
    last_error: str = ""
    # 本轮抓的 target 数 < 该 source 配置的总数 → 说明启用了分轮抓取。
    # 分片下每轮覆盖不同子集，"这一轮 0 条" 和上一轮的非零没有可比性。
    sharded: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "status": self.status,
            "reasons": list(self.reasons),
            "rounds": self.rounds,
            "last_round_at": self.last_round_at,
            "last_success_at": self.last_success_at,
            "last_nonzero_at": self.last_nonzero_at,
            "fail_streak": self.fail_streak,
            "zero_streak": self.zero_streak,
            "completeness_rate": self.completeness_rate,
            "last_complete_at": self.last_complete_at,
            "stale_full_scan_seconds": self.stale_full_scan_seconds,
            "circuit_open_rounds": self.circuit_open_rounds,
            "avg_listings": round(self.avg_listings, 1),
            "max_listings": self.max_listings,
            "last_listings": self.last_listings,
            "last_error": self.last_error,
            "sharded": self.sharded,
        }


def _judge(h: SourceHealth) -> None:
    """按指标定级，就地写回 status / reasons。"""
    reasons: list[str] = []

    if h.rounds == 0:
        h.status = STATUS_UNKNOWN
        h.reasons = ["窗口内没有遥测数据"]
        return

    down = False
    if h.fail_streak >= FAIL_STREAK_DOWN:
        down = True
        reasons.append(
            f"连续 {h.fail_streak} 轮抓取失败"
            + (f"（{h.last_error}）" if h.last_error else "")
        )

    # 「抓到 0 条」单独看没有意义——Xior 常态零可订，OurCampus 官网自述排队
    # 16–18 个月，把它们钉在告警上只会让真信号被噪音淹掉。
    #
    # 加上「该 source 自己在窗口内出现过非零」这个前提，规则就变成了
    # **本来有房、突然全没了**——这正是解析器被上游改版打坏的特征。
    #
    # ⚠️ 但**分轮抓取会打破这个前提**：分片之后每轮抓的是不同的 target 子集，
    # 某一轮 0 条只说明「这一片的楼没房」，和上一轮的非零根本不是同一批楼，
    # 没有可比性。2026-08-03 Xior 扩到 30 栋分片后立刻误报了一次。
    # 分片 source 的这条规则整个跳过，交给 fail_streak 和全局静默规则兜底。
    if h.sharded:
        pass
    elif h.zero_streak >= ZERO_STREAK_WARN and h.max_listings > 0:
        reasons.append(
            f"连续 {h.zero_streak} 轮零房源，但窗口内曾抓到 {h.max_listings} 条"
        )

    # 完整扫描率**不再**作为告警判据，只作为面板指标保留。分层抓取让它对 H2S
    # 结构性地停在 10% 上下，阈值永久为真。判据换成「多久没有完整轮」——量的是
    # stale 收敛滞后了多久，对分层与非分层 source 同样成立。见
    # STALE_FULL_SCAN_SECONDS 的注释。
    if h.stale_full_scan_seconds > STALE_FULL_SCAN_SECONDS:
        mins = h.stale_full_scan_seconds / 60
        reasons.append(
            f"已 {mins:.0f} 分钟没有完整扫描"
            f"（上限 {STALE_FULL_SCAN_SECONDS / 60:.0f} 分钟）"
            + (f"，最近一次 {fmt_ts(h.last_complete_at)}" if h.last_complete_at
               else "，库里没有任何完整轮的记录")
        )

    h.reasons = reasons
    if down:
        h.status = STATUS_DOWN
    elif reasons:
        h.status = STATUS_WARN
    else:
        h.status = STATUS_OK


def source_health_from_rows(
    source: str,
    rows: list[dict[str, Any]],
    *,
    last_complete_at: str = "",
    now: datetime | None = None,
) -> SourceHealth:
    """从遥测行算健康快照。``rows`` 必须**最新在前**。

    拆成纯函数是为了能脱离 DB 测试判级规则——规则本身才是容易写错的部分。

    ``last_complete_at`` 由调用方从库里查，**不能从 rows 里推**：窗口是按轮数
    截的（默认 24 轮），而 H2S 的全量间隔是 30 分钟、每轮约 90 秒，窗口时间跨度
    比全量间隔还短，里面常常一次全量都不含。据窗口判定必然误报。
    """
    h = SourceHealth(source=source, rounds=len(rows))
    if not rows:
        _judge(h)
        return h

    h.last_round_at = rows[0]["round_at"]
    h.last_listings = int(rows[0]["listings"])
    # 取最近一条**非熔断**错误：熔断轮不是失败，让它当 last_error 会使 down
    # 告警的正文指向「CircuitOpen」，而真正的成因（403 / 429）被盖住。
    h.last_error = next(
        (r["error_type"] for r in rows
         if r["error_type"] and r["error_type"] != CIRCUIT_OPEN_ERROR),
        "",
    )
    # total_targets 为 0 = 老行或未记录，按不分片处理（保守：规则照常生效）
    h.sharded = any(
        int(r.get("total_targets") or 0) > int(r.get("targets") or 0) for r in rows
    )

    for r in rows:
        if not r["error_type"] and not h.last_success_at:
            h.last_success_at = r["round_at"]
        if int(r["listings"]) > 0 and not h.last_nonzero_at:
            h.last_nonzero_at = r["round_at"]

    # 熔断跳过的那一轮**既不计失败也不打断计数**——它是按设计退避，不是抓取
    # 出错。原先把它当失败计入，后果是熔断器每正常工作一次就发一对
    # down + recovered 告警：Xior 的策略首次失败即跳闸，于是「1 次真失败 +
    # 2 轮熔断」凑够 3 轮，报出「⛔ 连续抓取失败｜错误 CircuitOpen」。
    # 保护装置一生效就拉警报，等于没有警报。
    #
    # 跳过而不是打断，是为了让**真实**失败仍能跨熔断期累积：
    # 失败 → 熔断 → canary 失败 → 熔断 → canary 失败，那是真的 down。
    h.circuit_open_rounds = sum(
        1 for r in rows if (r["error_type"] or "") == CIRCUIT_OPEN_ERROR
    )

    for r in rows:
        err = r["error_type"] or ""
        if err == CIRCUIT_OPEN_ERROR:
            continue
        if err:
            h.fail_streak += 1
        else:
            break

    # 失败轮**打断**零计数而不是并入：失败轮的 listings 恒为 0，若一并算进
    # zero_streak，任何一次失败都会顺带触发「零房源」告警，两条规则就重了。
    for r in rows:
        err = r["error_type"] or ""
        if err == CIRCUIT_OPEN_ERROR:
            continue
        if err:
            break
        if int(r["listings"]) == 0:
            h.zero_streak += 1
        else:
            break

    ok_rows = [r for r in rows if not r["error_type"]]
    if ok_rows:
        h.avg_listings = sum(int(r["listings"]) for r in ok_rows) / len(ok_rows)
        h.max_listings = max(int(r["listings"]) for r in ok_rows)
        targets = sum(int(r["targets"]) for r in ok_rows)
        if targets:
            h.completeness_rate = sum(int(r["complete"]) for r in ok_rows) / targets

    h.last_complete_at = last_complete_at or ""
    # 从未有过完整轮时，下界取窗口里**最早**那轮——能断言的只有「至少这么久
    # 没完整过」。取最新那轮会让下界恒为 0，规则等于不存在。
    h.stale_full_scan_seconds = _seconds_since(
        h.last_complete_at, rows[-1]["round_at"], now,
    )

    _judge(h)
    return h


def _seconds_since(
    last_complete_at: str, oldest_round_at: str, now: datetime | None,
) -> float:
    """距上次完整扫描过了多少秒；算不出返回 -1。

    基准取 ``now`` 而非 ``last_round_at``：进程若已经卡死不再写遥测，用最后一行
    当基准会让这个差值**永远停住**，恰好在最该报警时报不出来。

    从未有过完整轮（``last_complete_at`` 为空）时，用窗口内最早那轮当下界——
    答「至少这么久了」，而不是无从判断。库刚建起来、连一轮都没跑过的情况下
    两者都为空，返回 -1，由 rounds == 0 那条分支去判 UNKNOWN。
    """
    base = now or datetime.now(timezone.utc)
    ref = last_complete_at or oldest_round_at
    if not ref:
        return -1.0
    try:
        t = datetime.fromisoformat(ref)
    except ValueError:
        return -1.0
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return max(0.0, (base - t).total_seconds())


def source_health(storage, *, window: int = DEFAULT_WINDOW) -> list[SourceHealth]:
    """所有出现过的 source 的健康快照，按 source 名排序。"""
    out: list[SourceHealth] = []
    for src in storage.round_stats_sources():
        rows = storage.recent_round_stats(source=src, limit=window)
        try:
            last_complete = storage.last_complete_round_at(src)
        except Exception:
            # 老 Storage 或查询失败：退化成「算不出」，而不是把整个健康检查
            # 拖挂。stale 规则会因此静默，其余规则照常。
            last_complete = ""
        out.append(source_health_from_rows(
            src, rows, last_complete_at=last_complete,
        ))
    return out


def overall_status(healths: list[SourceHealth]) -> str:
    """整体取最差的那个。任何一个 source 塌了，整体就不是 ok。"""
    if not healths:
        return STATUS_UNKNOWN
    for want in (STATUS_DOWN, STATUS_WARN):
        if any(h.status == want for h in healths):
            return want
    if all(h.status == STATUS_UNKNOWN for h in healths):
        return STATUS_UNKNOWN
    return STATUS_OK


def silent_round_streak(storage, *, limit: int = 24) -> int:
    """从最近一轮往回数，连续几轮**全部 source 加起来都是 0 条**。

    这是 2026-06-13 起那次 7 周静默停摆的直接判据。当时进程活着、心跳正常、
    容器全程 healthy，唯一的异常就是「什么都没抓到」而没有任何东西在看这件事。
    """
    streak = 0
    for rnd in storage.recent_rounds_grouped(limit=limit):
        if rnd["listings"] > 0:
            break
        streak += 1
    return streak


def health_report(storage, *, window: int = DEFAULT_WINDOW) -> dict[str, Any]:
    """面板 / API 用的完整快照。"""
    healths = source_health(storage, window=window)
    return {
        "status": overall_status(healths),
        "window": window,
        "sources": [h.as_dict() for h in healths],
        "silent_round_streak": silent_round_streak(storage, limit=window),
        "thresholds": {
            "fail_streak_down": FAIL_STREAK_DOWN,
            "zero_streak_warn": ZERO_STREAK_WARN,
            "stale_full_scan_seconds": STALE_FULL_SCAN_SECONDS,
        },
    }
