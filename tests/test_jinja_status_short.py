"""
status_short Jinja filter 单元测试。

把 H2S 长状态字符串截短为 1 个词，让 listings / 首页 4 种状态胶囊
（Book / Lottery / Reserved / Occupied）视觉等宽。
"""
from __future__ import annotations

import pytest

from app.jinja_filters import status_short


@pytest.mark.parametrize("raw, expected", [
    # H2S 原始 status（最常见）
    ("Available to book", "Book"),
    ("Available in lottery", "Lottery"),
    ("available to book", "Book"),       # case insensitive
    ("AVAILABLE IN LOTTERY", "Lottery"),

    # 过渡态
    ("Reserved", "Reserved"),
    ("In Process", "Reserved"),
    ("Pending", "Reserved"),

    # 终态
    ("Occupied", "Occupied"),
    ("Rented", "Occupied"),
    ("Not available", "Occupied"),

    # 边界
    ("", ""),
    ("Unknown weird status", "Unknown weird status"),  # fallback 保留原文
])
def test_status_short(raw, expected):
    assert status_short(raw) == expected


def test_status_short_handles_none_gracefully():
    """传入 None 不应该崩——Jinja 可能传 Undefined / None。"""
    assert status_short(None) == ""  # type: ignore[arg-type]
