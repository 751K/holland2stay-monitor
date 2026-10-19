"""环境变量解析的唯一实现。

布尔与整数都收在这里，避免各模块各写一份：

- ``env_bool`` / ``parse_bool``：真值 1/true/yes/on，假值 0/false/no/off，
  空值或无法识别回落 ``default``。以前四套手写解析都不 ``strip()``，于是
  ``APNS_ENABLED=" true"`` 会被静默判成未启用。
- ``env_int``：空值回落 ``default``，非法值默认 WARNING + 回落；``strict``
  时抛错。``positive`` / ``minimum`` / ``maximum`` 覆盖旧的各套变体。
"""
from __future__ import annotations

import logging
import os

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})

logger = logging.getLogger("env_utils")


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


def env_int(
    name: str,
    default: int = 0,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
    positive: bool = False,
    strict: bool = False,
) -> int:
    """读一个整数环境变量。

    - 空值：``default``
    - 非法值：默认回落 ``default`` 并 WARNING；``strict=True`` 时抛 ValueError
    - ``positive=True``：非正值回落 ``default``（与旧的 _env_int_positive 一致）
    - ``minimum`` / ``maximum``：对有效值做夹取
    """
    raw = os.environ.get(name)
    text = (raw or "").strip()
    if not text:
        value = default
    else:
        try:
            value = int(text)
        except ValueError:
            if strict:
                raise ValueError(f"{name}={raw!r} 不是整数") from None
            logger.warning("%s=%r 不是整数，用默认值 %s", name, raw, default)
            return default
    if positive and value <= 0:
        if strict:
            raise ValueError(f"{name}={value} 必须是正整数") from None
        return default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value
