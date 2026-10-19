"""H2S REST 触发器：秒级发现新放出的房，叫醒主循环跑一轮只抓 H2S 的抓取。

整条链路
--------
::

    RestWatcher（自己的时间窗口内每 3 秒，一次请求；默认工作日 08:30–17:45）
      │  可订集合里出现了新 id
      ▼
    wake 事件 → 主循环提前结束等待
      │
      ▼
    run_once(only_sources={"holland2stay"})   ← 和平时一轮完全一样，只是提前了
      │  主抓取器（浏览器 + GraphQL）拿到完整数据
      ▼
    diff → 通知 → 自动预订（原路）

REST 只是触发器，不是数据源——为什么不让它直接写库或预订，见
``scrapers/holland2stay_rest.py`` 的模块文档。

什么时候跑
----------
有**自己的**时间窗口（``H2S_REST_START`` / ``H2S_REST_END`` /
``H2S_REST_WEEKDAYS_ONLY``，荷兰时间），和主循环的 PEAK_* 脱钩。

为什么不复用 PEAK_*：2026-09-18 ~ 10-18 Eindhoven 的 111 次非抽签放房里，
PEAK 那两段（工作日 08:30–10:00 / 13:30–15:00）只覆盖 30 次；工作日
08:30–17:45 覆盖 81 次——白天放房均匀铺满整个工作时间。可要是把 PEAK 拉宽，
主循环也会在这 9 个小时里按 15–60 秒跑，走代理的 GraphQL 流量翻两倍多。REST
一次请求 2 KB，拉宽它几乎不花钱；主循环的节奏是另一笔账。

出口
----
走代理池，每个请求换一个出口 IP（``rest_proxy``）。一天一万多个请求摊到池子里，
单个 IP 上稀疏得很；全压在服务器 IP 上则迟早被盯上，而服务器 IP 是代理全挂时
的兜底出口。两种情况**宁可不查**，退回主循环的节奏：

- 代理全在冷却（``get_proxy_url`` 返回空）——这时直连等于拿服务器 IP 硬扛
- 当前生效的是自家线路，且没开 ``H2S_REST_HOME_INTERVAL``——家里的 IP 不该被
  3 秒一次地磨

开了 ``H2S_REST_HOME_INTERVAL`` 时，自家线路上也查，但按这个更慢的间隔（下限
``HOME_MIN_INTERVAL``）。2026-10-16 起 webshare 一直 402，商业代理整个用不了，
主循环在自家线路上 120 秒一轮——快速通道整段停掉，正好是最需要它的时候。家宽上
被 H2S 拒一次（403 / 429 / 503）就在家宽上停 ``HOME_PAUSE_SEC``：自家 IP 同时
是主循环的出口，烧了没得换，宁可退回主循环的节奏。

REST 自己的失败**不**去标记代理故障：那会让一条只为提速而存在的旁路，把主抓取
共用的代理池打进冷却。

剩下那 24% 是每天 00:15–00:22 的夜间批（H2S 释放没付款的预留），平均能留十几
个小时、没人抢，主循环 5 分钟一轮足够，不值得为它开窗口。

窗口外一个请求都不发；离开窗口时丢掉基线，下次进窗口重新建——否则跨了一夜的
旧基线会把夜里所有变化当成「新出现」报一遍。

这些键都在 RUNTIME_KEYS：/settings 面板的「H2S 快速通道」里改，保存即触发热
重载、注水进环境变量；这里每个 tick 现读，不需要重启。

REST 看到了、GraphQL 还没看到
-----------------------------
两个接口背后的索引/缓存未必同步。被叫醒那一轮如果没抓到这些 id，就白跑了，
房源要等到下一次正常轮次。所以每个新 id 都记下来，轮次结束后去库里核对：没
出现的按 ``RETRY_DELAYS`` 再叫醒，补完还没有就放弃、交给正常轮次。核对结果
（含 REST 发现到入库的时延）都进日志，跑几天就知道这个时间差实际有多大。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from models import STATUS_AVAILABLE

logger = logging.getLogger(__name__)

H2S_SOURCE = "holland2stay"

ENABLED_ENV = "H2S_REST_ENABLED"
INTERVAL_ENV = "H2S_REST_INTERVAL"
START_ENV = "H2S_REST_START"
END_ENV = "H2S_REST_END"
WEEKDAYS_ENV = "H2S_REST_WEEKDAYS_ONLY"
HOME_INTERVAL_ENV = "H2S_REST_HOME_INTERVAL"

DEFAULT_START = "08:30"
DEFAULT_END = "17:45"

_AMS = ZoneInfo("Europe/Amsterdam")

DEFAULT_INTERVAL = 3.0
#: 下限。再快没有意义（主循环 1 秒一个 tick），只会给对方的限流递把柄。
MIN_INTERVAL = 1.0

#: 自家线路上的间隔下限。家宽只有一个 IP，和主循环共用。
HOME_MIN_INTERVAL = 10.0

#: 家宽上被 H2S 拒绝（403 / 429 / 503）后，家宽上停多久。商业代理恢复了照常查。
HOME_PAUSE_SEC = 3600.0

#: 窗口外 / 关闭时多久看一眼要不要开始。只是本地判断，不发请求。
IDLE_CHECK_SEC = 30.0

#: 派生代理 session 用的名字。配合 rotating=True 每次都是新 id，名字本身只用来
#: 和其它 source 区分开。
REST_PROXY_SOURCE = "holland2stay_rest"

#: 被叫醒那一轮没看到的 id，隔多久再叫醒一次。两次之后放弃。
RETRY_DELAYS: tuple[float, ...] = (5.0, 15.0)

#: 连续失败时每隔多少次汇总报一次。3 秒一轮，接口坏掉一小时就是 1200 次，
#: 逐次打 warning 会把日志刷满。
FAIL_LOG_EVERY = 100


def rest_enabled() -> bool:
    return (os.environ.get(ENABLED_ENV) or "").strip().lower() in (
        "1", "true", "yes", "on")


def rest_interval() -> float:
    raw = (os.environ.get(INTERVAL_ENV) or "").strip()
    try:
        val = float(raw) if raw else DEFAULT_INTERVAL
    except ValueError:
        logger.warning("%s=%r 不是数字，按默认 %.0f 秒", INTERVAL_ENV, raw, DEFAULT_INTERVAL)
        val = DEFAULT_INTERVAL
    return max(MIN_INTERVAL, val)


def rest_home_interval() -> float:
    """自家线路上的轮询间隔；0 = 自家线路上不查（默认）。"""
    raw = (os.environ.get(HOME_INTERVAL_ENV) or "").strip()
    try:
        val = float(raw) if raw else 0.0
    except ValueError:
        logger.warning("%s=%r 不是数字，自家线路上不查", HOME_INTERVAL_ENV, raw)
        return 0.0
    if val <= 0:
        return 0.0
    return max(HOME_MIN_INTERVAL, val)


def _hhmm(raw: str, default: str, key: str) -> int:
    """``"08:30"`` → 510（当天第几分钟）。格式不对按默认值，并说一声。"""
    text = (raw or "").strip() or default
    try:
        h, m = (int(x) for x in text.split(":"))
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
    except ValueError:
        logger.warning("%s=%r 不是 HH:MM，按默认 %s", key, raw, default)
        h, m = (int(x) for x in default.split(":"))
    return h * 60 + m


def rest_window() -> tuple[int, int, bool]:
    """(开始分钟, 结束分钟, 仅工作日)。"""
    start = _hhmm(os.environ.get(START_ENV, ""), DEFAULT_START, START_ENV)
    end = _hhmm(os.environ.get(END_ENV, ""), DEFAULT_END, END_ENV)
    weekdays = (os.environ.get(WEEKDAYS_ENV) or "true").strip().lower() not in (
        "0", "false", "no", "off")
    return start, end, weekdays


def in_rest_window(now: Optional[datetime] = None) -> bool:
    """现在（荷兰时间）在不在 REST 的时间窗口里。两端都含。

    开始晚于结束表示跨午夜（例如 22:00–02:00）。仅工作日时按**当前**这一刻是
    星期几判，所以跨午夜的窗口在周五夜里过了 0 点就停。
    """
    now = (now or datetime.now(_AMS)).astimezone(_AMS)
    start, end, weekdays = rest_window()
    if weekdays and now.weekday() >= 5:
        return False
    cur = now.hour * 60 + now.minute
    if start <= end:
        return start <= cur <= end
    return cur >= start or cur <= end


def _personal_active() -> bool:
    from config import is_personal_proxy_active
    return is_personal_proxy_active()


def rest_proxy(*, home: bool = False) -> str:
    """这次 REST 查询用的代理 URL；空串 = 现在不该查（见模块文档「出口」）。

    ``home=True``：调用方已确认可以走家宽。当前是自家线路时返回它本身——不带
    source 取，带 source 可能拿到这个 source 自己还没冷却、实际已经坏掉的商业代理。
    """
    from config import get_proxy_url, is_personal_proxy_active

    if is_personal_proxy_active():
        return get_proxy_url() if home else ""
    return get_proxy_url(REST_PROXY_SOURCE, rotating=True)


def _is_pushback(err: Optional[str]) -> bool:
    """H2S 明确拒绝了这个出口（不是网络抖动、不是响应变形）。"""
    return bool(err) and (err.startswith("HTTP 403")
                          or "HTTP 429" in err or "HTTP 503" in err)


def h2s_city_ids(cfg) -> tuple[int, ...]:
    """当前配置里要抓的 H2S 城市 ID。跟着 SOURCES / CITIES 走，不另设。"""
    out: set[int] = set()
    for t in cfg.scrape_tasks_v2():
        if t.source != H2S_SOURCE:
            continue
        try:
            out.add(int(t.city_key))
        except (TypeError, ValueError):
            logger.debug("H2S 城市 key 不是数字，REST 跳过: %r", t.city_key)
    return tuple(sorted(out))


def _parse_ts(raw) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class RestTrigger:
    """REST 轮询 + 待核对 id 的记账。

    monitor 侧的用法::

        trigger = RestTrigger(wake_event)
        asyncio.create_task(trigger.run(lambda: cfg))
        ...
        ids = trigger.begin_round()          # 每轮开头
        await run_once(...)
        trigger.end_round(ids, storage, started_at)   # 抓取成功后

    ``begin_round`` 不会把 id 移出待核对表：这一轮抛异常、没走到
    ``end_round`` 时，它们留着给下一轮核对。
    """

    def __init__(
        self,
        wake: asyncio.Event,
        *,
        watcher_factory: Optional[Callable[..., object]] = None,
        clock: Callable[[], float] = time.monotonic,
        schedule: Optional[Callable[[float, Callable[[], None]], object]] = None,
    ) -> None:
        self.wake = wake
        self._watcher_factory = watcher_factory
        self._clock = clock
        self._schedule = schedule

        self._watcher = None
        self._watcher_key: tuple[int, ...] = ()

        #: id → {"name", "found_at"(monotonic), "attempts", "due"(monotonic)}
        self.pending: dict[str, dict] = {}

        self.fail_streak = 0

        #: 本次查询是否在自家线路上（每个 tick 开头判一次）
        self.on_home = False
        self._home_paused_until = float("-inf")

    # ------------------------------------------------------------------ #
    # 出口
    # ------------------------------------------------------------------ #
    def _home_allowed(self) -> bool:
        return rest_home_interval() > 0 and self._clock() >= self._home_paused_until

    def _proxy(self) -> str:
        if self.on_home and not self._home_allowed():
            return ""
        return rest_proxy(home=self.on_home)

    def _note_exit(self, home: bool) -> None:
        if home == self.on_home:
            return
        self.on_home = home
        if not home:
            logger.info("[h2s-rest] 商业代理恢复，回到每 %.0f 秒一次", rest_interval())
        elif rest_home_interval() > 0:
            logger.info("[h2s-rest] 当前走自家线路：每 %.0f 秒一次（被 H2S 拒绝就在家宽上停 %d 分钟）",
                        rest_home_interval(), HOME_PAUSE_SEC // 60)
        else:
            logger.info("[h2s-rest] 当前是自家线路，没开 %s，暂停", HOME_INTERVAL_ENV)

    # ------------------------------------------------------------------ #
    # 轮询
    # ------------------------------------------------------------------ #
    def _new_watcher(self, city_ids: tuple[int, ...]):
        if self._watcher_factory is not None:
            return self._watcher_factory(city_ids)
        from scrapers.holland2stay_rest import RestWatcher
        return RestWatcher(city_ids, proxy=self._proxy)

    def _drop_watcher(self, why: str) -> None:
        if self._watcher is None:
            return
        try:
            self._watcher.close()
        except Exception:
            pass
        self._watcher = None
        self._watcher_key = ()
        if self.fail_streak:
            self.fail_streak = 0
        logger.info("[h2s-rest] 停止轮询（%s），基线已丢弃", why)

    async def tick(self, cfg) -> float:
        """跑一次（或判定这次不该跑），返回到下一次该等多少秒。"""
        if not rest_enabled():
            self._drop_watcher("已关闭")
            return IDLE_CHECK_SEC
        if not in_rest_window():
            self._drop_watcher("不在时间窗口")
            return IDLE_CHECK_SEC
        city_ids = h2s_city_ids(cfg)
        if not city_ids:
            self._drop_watcher("没有 H2S 城市")
            return IDLE_CHECK_SEC

        if self._watcher is None or self._watcher_key != city_ids:
            if self._watcher is not None:
                self._drop_watcher("城市配置变了")
            self._watcher = self._new_watcher(city_ids)
            self._watcher_key = city_ids
            logger.info("[h2s-rest] 开始轮询：城市 %s，每 %.0f 秒一次",
                        ",".join(map(str, city_ids)), rest_interval())

        self._note_exit(_personal_active())
        interval = rest_interval()
        if self.on_home:
            interval = max(interval, rest_home_interval())
        t0 = self._clock()
        watcher = self._watcher
        new = await asyncio.to_thread(watcher.poll)
        self._note_health(watcher)
        if self.on_home and _is_pushback(getattr(watcher, "last_error", None)):
            self._home_paused_until = self._clock() + HOME_PAUSE_SEC
            logger.warning("[h2s-rest] 自家线路被 H2S 拒绝（%s），家宽上暂停 %d 分钟——主循环照常",
                           watcher.last_error, HOME_PAUSE_SEC // 60)
        if new:
            self.note_new(new)
        return max(0.0, interval - (self._clock() - t0))

    def _note_health(self, watcher) -> None:
        err = getattr(watcher, "last_error", None)
        if err:
            self.fail_streak += 1
            if self.fail_streak == 1 or self.fail_streak % FAIL_LOG_EVERY == 0:
                logger.warning("[h2s-rest] 查询失败（连续 %d 次）: %s——这段时间退回主循环的节奏",
                               self.fail_streak, err)
        elif self.fail_streak:
            logger.info("[h2s-rest] 查询恢复（之前连续失败 %d 次）", self.fail_streak)
            self.fail_streak = 0

    def note_new(self, records: list[dict]) -> None:
        now = self._clock()
        added = []
        for rec in records:
            lid = rec.get("id")
            if not lid or lid in self.pending:
                continue
            self.pending[lid] = {
                "name": rec.get("name") or lid,
                "found_at": now,
                "found_wall": datetime.now(timezone.utc),
                "attempts": 0,
                "due": now,
            }
            added.append(f"{rec.get('name') or lid}（€{rec.get('price')}）")
        if added:
            logger.info("⚡ [h2s-rest] 新出现 %d 套可订: %s——叫醒主循环",
                        len(added), "、".join(added))
            self.wake.set()

    async def run(self, get_cfg: Callable[[], object]) -> None:
        """常驻循环。任何异常都吞掉继续：它挂了不能连累主循环。"""
        while True:
            try:
                delay = await self.tick(get_cfg())
            except asyncio.CancelledError:
                self._drop_watcher("monitor 退出")
                raise
            except Exception:
                logger.exception("[h2s-rest] tick 异常（已忽略）")
                delay = IDLE_CHECK_SEC
            await asyncio.sleep(delay)

    # ------------------------------------------------------------------ #
    # 与主循环的握手
    # ------------------------------------------------------------------ #
    def has_due(self) -> bool:
        now = self._clock()
        return any(p["due"] <= now for p in self.pending.values())

    def begin_round(self) -> list[str]:
        """本轮要核对的 id（已到期的那些）。不移出待核对表。"""
        now = self._clock()
        return [lid for lid, p in self.pending.items() if p["due"] <= now]

    def end_round(self, ids: list[str], storage, started_at: datetime) -> None:
        """抓取成功之后核对：库里出现了（本轮刷新、状态可订）就结案，否则排补抓。"""
        if not ids:
            return
        now = self._clock()
        # 时间戳的精度和时钟误差留 2 秒余量：last_seen 是 diff() 写库那一刻，
        # 一定晚于 started_at；这里只防「同一秒内」的比较抖动。
        floor = started_at - timedelta(seconds=2)
        retry_due: list[float] = []
        for lid in ids:
            p = self.pending.get(lid)
            if p is None:
                continue
            row = None
            try:
                row = storage.get_listing(lid)
            except Exception:
                logger.debug("核对 %s 时读库失败", lid, exc_info=True)
            seen_at = _parse_ts(row.get("last_seen")) if row else None
            status = (row.get("status") or "").strip().lower() if row else ""
            if seen_at is not None and seen_at >= floor and status == STATUS_AVAILABLE:
                lag = (seen_at - p["found_wall"]).total_seconds()
                logger.info("[h2s-rest] %s：REST 发现 → 主抓取入库 %.1f 秒（第 %d 次核对）",
                            p["name"], lag, p["attempts"] + 1)
                del self.pending[lid]
                continue
            p["attempts"] += 1
            if p["attempts"] > len(RETRY_DELAYS):
                logger.warning(
                    "[h2s-rest] %s：REST 看到可订，主抓取 %d 次都没看到（库里状态 %s），"
                    "放弃补抓，交给正常轮次",
                    p["name"], p["attempts"], status or "无记录")
                del self.pending[lid]
                continue
            delay = RETRY_DELAYS[p["attempts"] - 1]
            p["due"] = now + delay
            retry_due.append(delay)
            logger.info("[h2s-rest] %s：主抓取这一轮还没看到（库里状态 %s），%.0f 秒后补抓",
                        p["name"], status or "无记录", delay)
        for delay in sorted(set(retry_due)):
            self._schedule_wake(delay)

    def _schedule_wake(self, delay: float) -> None:
        if self._schedule is not None:
            self._schedule(delay, self.wake.set)
            return
        try:
            asyncio.get_running_loop().call_later(delay, self.wake.set)
        except RuntimeError:
            # 没有运行中的事件循环（单元测试直接调）：不排定时器，id 留在表里，
            # 下一轮照样会核对。
            logger.debug("无事件循环，补抓定时器未排")
