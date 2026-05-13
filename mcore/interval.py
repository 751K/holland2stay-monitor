"""智能轮询间隔计算（纯函数，无共享状态）。"""

from __future__ import annotations

import random
from datetime import datetime
from zoneinfo import ZoneInfo

_AMS = ZoneInfo("Europe/Amsterdam")


def get_interval(cfg) -> tuple[int, bool]:
    """
    根据荷兰本地时间（Europe/Amsterdam）判断当前是否处于高峰期。

    Returns
    -------
    (interval_seconds, is_peak)
    interval_seconds : 本轮应等待的基准秒数（抖动前）
    is_peak          : True 表示当前处于高峰期
    """
    now = datetime.now(_AMS)
    if cfg.peak_weekdays_only and now.weekday() >= 5:
        return cfg.check_interval, False

    cur = now.hour * 60 + now.minute

    def _in_window(start: str, end: str) -> bool:
        try:
            sh, sm = map(int, start.split(":"))
            eh, em = map(int, end.split(":"))
        except ValueError:
            return False
        return sh * 60 + sm <= cur <= eh * 60 + em

    if _in_window(cfg.peak_start, cfg.peak_end) or _in_window(
        cfg.peak_start_2, cfg.peak_end_2
    ):
        return cfg.peak_interval, True
    return cfg.check_interval, False


def apply_jitter(seconds: int, ratio: float = 0.20) -> int:
    """
    在基准等待时间上叠加随机抖动，避免多实例在同一秒发起请求。

    Parameters
    ----------
    seconds : 基准等待时间（秒）
    ratio   : 抖动比例（0–0.5），来自 cfg.jitter_ratio；
              e.g. 0.20 → 实际时间在 [seconds*0.8, seconds*1.2] 内随机

    Returns
    -------
    实际等待时间（秒），最小 5 秒
    """
    delta = seconds * ratio
    return max(5, int(seconds + random.uniform(-delta, delta)))
