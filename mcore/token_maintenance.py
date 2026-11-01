"""Periodic cleanup of expired app sessions and their associated devices."""
import logging
import time

logger = logging.getLogger("monitor")
_CLEANUP_INTERVAL = 24 * 60 * 60


class TokenMaintenance:
    def __init__(self):
        self._last_success = None

    def run_if_due(self, storage):
        now = time.monotonic()
        if self._last_success is not None and now - self._last_success < _CLEANUP_INTERVAL:
            return
        try:
            deleted = storage.cleanup_expired_tokens(keep_revoked_days=30)
        except Exception:
            logger.exception("清理过期会话失败，下轮重试")
            return
        self._last_success = now
        if deleted:
            logger.info("已清理 %d 个过期或撤销超过 30 天的会话及关联设备", deleted)
