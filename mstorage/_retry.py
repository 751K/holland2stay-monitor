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
        result: dict[str, set[str]] = {}
        for user_id, ids in data.items():
            # 兼容旧格式：list → set
            result[user_id] = set(ids) if isinstance(ids, list) else ids
        return result

    def save_retry_queue(self, queue: dict[str, set[str]]) -> None:
        if not queue:
            self.set_meta("retry_queue", "")
            return
        serializable = {uid: sorted(lst) for uid, lst in queue.items()}
        self.set_meta("retry_queue", json.dumps(serializable, ensure_ascii=False))
