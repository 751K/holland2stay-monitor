"""竞败重试队列的持久化（meta 表中的 retry_queue key）。"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


class RetryQueueOps:
    """依赖 self.get_meta / self.set_meta（来自 StorageBase）。"""

    def load_retry_queue(self) -> dict[str, set[str]]:
        raw = self.get_meta("retry_queue", "")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.warning("retry_queue JSON 损坏，已重置")
            self.set_meta("retry_queue", "")
            return {}
        if not isinstance(data, dict):
            logger.warning("retry_queue JSON 顶层类型异常 (%s)，已重置", type(data).__name__)
            self.set_meta("retry_queue", "")
            return {}
        result: dict[str, set[str]] = {}
        for user_id, ids in data.items():
            if isinstance(ids, list):
                result[user_id] = set(ids)
            elif isinstance(ids, set):
                result[user_id] = ids
            else:
                logger.warning(
                    "retry_queue[%s] 类型异常 (%s)，已跳过", user_id, type(ids).__name__
                )
        return result

    def save_retry_queue(self, queue: dict[str, set[str]]) -> None:
        if not queue:
            self.set_meta("retry_queue", "")
            return
        serializable = {uid: sorted(lst) for uid, lst in queue.items()}
        self.set_meta("retry_queue", json.dumps(serializable, ensure_ascii=False))
