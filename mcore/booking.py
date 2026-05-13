"""
自动预订逻辑 + 竞争失败重试队列。

book_with_fallback
    按面积降序对候选房源依次调用 try_book()，race_lost 时自动尝试备选。

RetryQueue
    竞争失败时存储候选 listing_id，下轮恢复并重试。
    持久化到 SQLite meta 表，进程重启不丢失。
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from booker import try_book
from models import parse_float

if TYPE_CHECKING:
    from users import UserConfig

logger = logging.getLogger("monitor")


# ------------------------------------------------------------------ #
# 工具
# ------------------------------------------------------------------ #


def area_key(listing) -> float:
    """
    从 Listing.feature_map() 提取面积数值，用于多套候选时按面积降序选最大。

    Returns
    -------
    float 面积值（m²）；无法解析时返回 0.0（排在最后）
    """
    area_str = listing.feature_map().get("area", "")
    val = parse_float(area_str)
    return val if val is not None else 0.0


# ------------------------------------------------------------------ #
# 预订回退逻辑
# ------------------------------------------------------------------ #


def book_with_fallback(
    sorted_candidates: list,
    user: "UserConfig",
    deadline: float,
    prewarmed=None,
):
    """
    按面积降序依次对 sorted_candidates 中的房源尝试 try_book()。

    重试条件
    --------
    仅在 result.phase == "race_lost"（房源已被他人抢先预订）时继续尝试下一套。
    其余失败类型（reserved_conflict / unknown_error 等）立即返回——这些错误
    与具体房源无关，换一套也无法解决。

    截止时间
    --------
    第一套无条件尝试，确保用户不会因截止超时而错过所有机会。
    从第二套起，仅在 deadline 之前继续，避免占用下一轮扫描的时间窗口。
    deadline = float('inf') 表示无限制（--once / --test 模式）。
    """
    last_result = None
    for i, listing in enumerate(sorted_candidates):
        if i > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning(
                    "[%s] ⏰ 已到下次扫描截止，停止备选重试（已尝试 %d/%d，剩余 %d 套未试）",
                    user.name, i, len(sorted_candidates),
                    len(sorted_candidates) - i,
                )
                break
            logger.info(
                "[%s] 🔄 竞争失败，尝试备选 %d/%d: %s (%.1f m²)，距截止还剩 %.0f 秒",
                user.name, i + 1, len(sorted_candidates),
                listing.name, area_key(listing), remaining,
            )

        result = try_book(
            listing,
            user.auto_book.email,
            user.auto_book.password,
            dry_run=user.auto_book.dry_run,
            cancel_enabled=user.auto_book.cancel_enabled,
            payment_method=user.auto_book.payment_method,
            prewarmed=prewarmed,
        )
        last_result = result

        if result.success or result.dry_run or result.phase != "race_lost":
            return result

    return last_result


# ------------------------------------------------------------------ #
# 重试队列
# ------------------------------------------------------------------ #


class RetryQueue:
    """
    竞争失败重试队列。

    背景
    ----
    storage.diff() 只产出"新增"和"状态变更"两类事件。如果一套房子在上轮就是
    "Available to book" 且状态未变（如前一个预订者未付款、房子重新放出），
    它既不进 new_listings 也不进 status_changes，自动预订永远不会重试。

    try_book 竞争失败（race_lost）时，将候选 listing_id 加入此队列。
    每轮 run_once 开始时，检查队列中的 ID 是否仍在本次抓取的 "Available to book"
    列表中，若是则直接加入 ab_candidates，触发新一轮预订尝试。

    持久化
    ------
    队列通过 Storage.save_retry_queue() 落盘到 SQLite meta 表，
    进程重启后由 load() 恢复。
    """

    def __init__(self) -> None:
        self._queue: dict[str, set[str]] = {}
        self._dirty = False

    # -- 持久化 --------------------------------------------------------

    def load(self, storage) -> None:
        self._queue = storage.load_retry_queue()
        if self._queue:
            total = sum(len(v) for v in self._queue.values())
            logger.info("已恢复重试队列: %d 个用户, %d 套候选", len(self._queue), total)

    def save(self, storage) -> None:
        if not self._dirty:
            return
        storage.save_retry_queue(self._queue)
        self._dirty = False

    # -- 读写 ----------------------------------------------------------

    def get(self, user_id: str) -> set[str]:
        return self._queue.get(user_id, set())

    def add(self, user_id: str, listing_ids: set) -> None:
        self._queue.setdefault(user_id, set()).update(listing_ids)
        self._dirty = True

    def discard(self, user_id: str, listing_id: str) -> None:
        if user_id in self._queue:
            self._queue[user_id].discard(listing_id)
            if not self._queue[user_id]:
                del self._queue[user_id]
            self._dirty = True

    def remove_gone(self, user_id: str, gone_ids: set) -> None:
        """批量移除已不在可用列表中的 ID（原地修改）。"""
        user_set = self._queue.get(user_id)
        if not user_set:
            return
        user_set -= gone_ids
        if not user_set:
            del self._queue[user_id]
        self._dirty = True

    def __bool__(self) -> bool:
        return bool(self._queue)
