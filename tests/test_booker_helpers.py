"""
booker 辅助函数测试 — 预订流程的关键守卫逻辑。

纯函数，零依赖，覆盖：
- _is_booked_by_other   : 竞争失败文案识别（子串匹配 H2S 英文）
- _is_reserved_by_user  : 预留单冲突文案识别（多模式子串）
- _to_h2s_date          : ISO→DD-MM-YYYY 日期格式转换
"""
from __future__ import annotations

import pytest

from booker import _is_booked_by_other, _is_reserved_by_user, _to_h2s_date


# ── _is_booked_by_other ────────────────────────────────────

class TestIsBookedByOther:
    def test_exact_h2s_message(self):
        assert _is_booked_by_other(
            "Sorry, the residence you have selected is already booked by someone else."
        )

    def test_substring_in_longer_error(self):
        assert _is_booked_by_other(
            "[placeOrder] 下单失败: [ERR_001] already booked by someone else. Please try again."
        )

    def test_case_insensitive(self):
        assert _is_booked_by_other(
            "ALREADY BOOKED BY SOMEONE ELSE"
        )

    def test_different_reason_returns_false(self):
        assert not _is_booked_by_other("another unit reserved")

    def test_empty_string(self):
        assert not _is_booked_by_other("")

    def test_none_like_value(self):
        """None 会在调用 .lower() 时抛异常——调用方保证非空。"""
        with pytest.raises(AttributeError):
            _is_booked_by_other(None)  # type: ignore[arg-type]


# ── _is_reserved_by_user ───────────────────────────────────

class TestIsReservedByUser:
    def test_another_unit_reserved(self):
        assert _is_reserved_by_user(
            "Sorry, at the moment you have another unit reserved."
        )

    def test_you_have_another(self):
        assert _is_reserved_by_user(
            "Place order failed: you have another order pending."
        )

    def test_at_the_moment_you_have(self):
        assert _is_reserved_by_user(
            "at the moment you have an active reservation"
        )

    def test_case_insensitive(self):
        assert _is_reserved_by_user(
            "YOU HAVE ANOTHER UNIT RESERVED"
        )

    def test_substring_in_longer_message(self):
        assert _is_reserved_by_user(
            "[placeOrder] Error: another unit reserved for this account. Cancel first."
        )

    def test_race_lost_message_returns_false(self):
        assert not _is_reserved_by_user(
            "already booked by someone else"
        )

    def test_unrelated_error(self):
        assert not _is_reserved_by_user("Internal server error")

    def test_empty_string(self):
        assert not _is_reserved_by_user("")


# ── _to_h2s_date ───────────────────────────────────────────

class TestToH2sDate:
    def test_standard_date(self):
        assert _to_h2s_date("2026-05-04") == "04-05-2026"

    def test_year_boundary(self):
        assert _to_h2s_date("2025-12-31") == "31-12-2025"

    def test_january(self):
        assert _to_h2s_date("2027-01-01") == "01-01-2027"

    def test_empty_string_raises(self):
        with pytest.raises(ValueError, match="不能为空"):
            _to_h2s_date("")

    def test_none_raises(self):
        with pytest.raises(ValueError, match="不能为空"):
            _to_h2s_date(None)  # type: ignore[arg-type]

    def test_wrong_format_raises(self):
        with pytest.raises(ValueError, match="日期格式错误"):
            _to_h2s_date("04-05-2026")  # DD-MM-YYYY 不是 ISO

    def test_garbage_string_raises(self):
        with pytest.raises(ValueError, match="日期格式错误"):
            _to_h2s_date("not-a-date")

    def test_short_string_raises(self):
        with pytest.raises(ValueError, match="日期格式错误"):
            _to_h2s_date("2026")
