"""
mcore.geocode — 地址 → 坐标
============================

从 app/routes/map_routes.py 提出来，因为它现在有两个调用方：

- Web 侧的手动触发（管理员点「解析坐标」，带进度状态）
- 监控进程的周期任务（每隔一段时间补齐新房源的坐标）

放在 ``mcore`` 而不是 ``app/services``：monitor.py 不 import ``app.*``，
共享代码放进 app 层会把依赖方向倒过来。

外部服务
--------
Photon（photon.komoot.io），OpenStreetMap 数据，无需 API key。走
``net.direct_urlopen``——**不经抓取代理**。地理编码请求里带的是房源地址，
和用户数据一样不该借道共享出口；而且 Photon 不在任何反爬后面，走代理只是
白白多一跳。

节流
----
每个地址之间至少间隔 ``_MIN_INTERVAL`` 秒。Photon 是免费公共服务，没有公开
的速率上限，这个间隔是自觉的下限而不是被逼出来的。
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

#: 相邻两次请求的最小间隔（秒）。
_MIN_INTERVAL = 0.15

#: 单次周期任务最多解析多少个地址。不设上限的话，第一次跑会对着几百个地址
#: 连打几分钟——那不该发生在监控轮次里。剩下的交给下一次。
DEFAULT_BATCH = 30


def geocode_one(addr: str) -> Optional[tuple[float, float]]:
    """单个地址 → (lat, lng)；解析不出返回 None。

    含 Room 房号的地址（如 "Westblaak 924 Room 2"）Photon 往往无结果，
    失败时去掉房号按建筑地址再试一次。
    """
    from urllib.parse import quote
    from urllib.request import Request

    from net import direct_urlopen

    def _query(q: str) -> Optional[tuple[float, float]]:
        url = f"https://photon.komoot.io/api/?q={quote(q)}&limit=1"
        req = Request(url, headers={"User-Agent": "FlatRadar/1.0"})
        resp = direct_urlopen(req, timeout=5)
        data = json.loads(resp.read().decode())
        feats = data.get("features", [])
        if feats:
            coords = feats[0]["geometry"]["coordinates"]
            return float(coords[1]), float(coords[0])   # (lat, lng)
        return None

    result = _query(addr)
    if result is not None:
        return result

    stripped = re.sub(r"\bRoom\s+\S+", "", addr, flags=re.IGNORECASE).strip().rstrip(",")
    if stripped != addr:
        try:
            return _query(stripped)
        except Exception:
            pass
    return None


def geocode_addresses(
    storage,
    addresses: list[str],
    *,
    on_progress: Optional[Callable[[int, int, list[dict]], None]] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[int, int, list[dict]]:
    """逐个解析并写入坐标缓存。返回 ``(成功数, 失败数, 错误列表)``。

    ``on_progress(done, failed, errors)`` 每处理完一个地址调用一次，供 Web
    侧刷新进度；监控进程不传。

    单个地址失败不中断整批——一条解析不出的地址不该让后面几十条也拿不到坐标。
    """
    done = failed = 0
    errors: list[dict] = []

    for addr in addresses:
        try:
            coord = geocode_one(addr)
            if coord:
                storage.cache_coords(addr, coord[0], coord[1])
                done += 1
            else:
                failed += 1
                errors.append({"address": addr, "reason": "Photon returned no results"})
        except Exception as exc:
            failed += 1
            # 异常文本里带着 Photon 的服务地址，而进度接口是 @api_login_required
            # ——普通用户读得到。只归类，详情进日志。
            logger.exception("geocode failed for %r: %s", addr, exc)
            errors.append({"address": addr, "reason": "geocoding request failed"})
        if on_progress is not None:
            on_progress(done, failed, errors)
        sleep(_MIN_INTERVAL)

    return done, failed, errors


def geocode_missing(storage, *, limit: int = DEFAULT_BATCH) -> tuple[int, int]:
    """把还没有坐标的房源补上，最多 ``limit`` 个。返回 ``(成功数, 失败数)``。

    给监控进程的周期任务用。``limit`` 是必须的：稳态下每轮只有零星几个新地址，
    但第一次跑（或换了监控城市之后）会有几百个，不设上限就会把一个抓取轮次
    拖成几分钟。

    没有待解析地址时不产生任何外部请求，也不写库。
    """
    listings = storage.get_map_listings()
    pending: list[str] = []
    seen: set[str] = set()
    for l in listings:
        addr = l.get("address") or ""
        if not addr or addr in seen:
            continue
        seen.add(addr)
        if not storage.get_cached_coords(addr):
            pending.append(addr)
            if len(pending) >= limit:
                break

    if not pending:
        return 0, 0

    done, failed, _ = geocode_addresses(storage, pending)
    logger.info("地理编码补齐：成功 %d，失败 %d，本批 %d 个地址",
                done, failed, len(pending))
    return done, failed
