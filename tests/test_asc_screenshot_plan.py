"""替换一组 App Store 截图的操作顺序。

这个顺序被改过两次，两次都是被实跑打回来的：

1. **先删后传**——第一次实跑就把 en-US 下用户手动传的八张删光，而紧接着的
   PUT 400 失败，集合被清空，什么都没剩下。
2. **先传后删**（为修上面那条）——安全，但有它自己的边界：App Store Connect
   每组上限 10 张，``existing + incoming`` 超过就永远传不完。6 张旧的 + 7 张
   新的 = 13，传到第 4 张拿到

       Too many screenshots. | Set: … has already 10 appScreenshots

   而这个错误只在真正对着一个已有 6 张的集合跑时才会出现——空集合上怎么试
   都是绿的。

现在是边传边删。这些断言守的就是那两条边界。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _mod():
    path = ROOT / "tools" / "asc" / "asc_screenshots.py"
    spec = importlib.util.spec_from_file_location("asc_screenshots", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


plan = None


def setup_module(_):
    global plan
    plan = _mod().plan_replacement


def _live_counts(ops, existing, cap=10):
    """回放序列，返回每一步之后集合里的张数。"""
    live = existing
    out = []
    for op in ops:
        live += 1 if op == "upload" else -1
        out.append(live)
    return out


def test_empty_set_just_uploads():
    assert plan(0, 3) == ["upload"] * 3


def test_all_incoming_are_uploaded_and_all_existing_deleted():
    ops = plan(6, 7)
    assert ops.count("upload") == 7
    assert ops.count("delete") == 6


def test_never_exceeds_the_cap():
    """撞顶就是那个 409。"""
    for existing in range(0, 11):
        for incoming in range(1, 11):
            ops = plan(existing, incoming)
            assert max(_live_counts(ops, existing)) <= 10, (
                f"existing={existing} incoming={incoming} 超过了 10 张上限")


def test_never_empties_the_set_while_old_ones_are_still_needed():
    """中途失败时集合里必须还有东西——那是「先删后传」毁掉八张图的教训。"""
    for existing in range(1, 11):
        for incoming in range(1, 11):
            ops = plan(existing, incoming)
            assert min(_live_counts(ops, existing)) >= 1, (
                f"existing={existing} incoming={incoming} 中途把集合清空了")


def test_the_replacement_that_actually_failed():
    """6 张旧的 + 7 张新的——上一版就是在这组数字上 409 的。"""
    ops = plan(6, 7)
    live = _live_counts(ops, 6)
    assert max(live) <= 10
    assert min(live) >= 1
    assert live[-1] == 7
    # 前四张有空位可以直接传，第五张之前必须先腾一个
    assert ops[:5] == ["upload", "upload", "upload", "upload", "delete"]


def test_incoming_larger_than_the_cap_is_rejected_not_silently_truncated():
    with pytest.raises(ValueError):
        plan(0, 11)
