"""环境变量解析的唯一实现。

以前布尔开关散落在 config / notifier / app.auth / apns / fcm / web 各处，
各自 ``lower() == "true"`` 或 ``!= "false"`` 且都不 ``strip()``。于是
``APNS_ENABLED=" true"`` 会被静默判成未启用。这里统一成：

- 真值：1 / true / yes / on
- 假值：0 / false / no / off
- 空值或无法识别：回落 ``default``

对默认开的关键开关（WEB_GUEST_MODE 等），无法识别的值回落 True，保持旧行为；
对默认关的开关回落 False。
"""
from __future__ import annotations

import os

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def parse_bool(raw, default: bool = False) -> bool:
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if not text:
        return default
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return default


def env_bool(name: str, default: bool = False) -> bool:
    return parse_bool(os.environ.get(name), default)
