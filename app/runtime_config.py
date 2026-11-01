"""A coherent, validated saved runtime configuration for each Web request."""
from __future__ import annotations

import logging
import threading
from types import MappingProxyType

from flask import has_request_context, request

logger = logging.getLogger(__name__)
_lock = threading.Lock()
_last_good_key = None
_last_good = None


def web_runtime_environment():
    """Resolve once per request without changing process-global configuration.

    Web views reflect saved configuration, not monitor acknowledgement. A bad
    candidate keeps this worker's last validated snapshot until a later request
    succeeds. Separate workers independently resolve the same persisted values.
    """
    import config
    from app.db import storage
    from settings_store import prepare_runtime, runtime_environment
    from target_config import validate

    if not has_request_context():
        return runtime_environment()
    if "flatradar.runtime_environment" in request.environ:
        return request.environ["flatradar.runtime_environment"]

    global _last_good_key, _last_good
    key = (str(config.DB_PATH), str(config.ENV_PATH))
    with _lock:
        try:
            candidate = prepare_runtime(storage(), env_path=config.ENV_PATH)
            errors = [p for p in validate(dict(candidate.values)) if p.fatal]
            if errors:
                raise ValueError("invalid runtime configuration")
            environment = runtime_environment(candidate)
            config.load_config(environ=environment)
            snapshot = MappingProxyType(environment)
            _last_good_key, _last_good = key, snapshot
        except Exception:
            logger.warning("Web 运行配置读取失败，沿用上次可用配置", exc_info=True)
            snapshot = _last_good if _last_good_key == key else None
            if snapshot is None:
                snapshot = MappingProxyType(runtime_environment())
        request.environ["flatradar.runtime_environment"] = snapshot
        return snapshot
