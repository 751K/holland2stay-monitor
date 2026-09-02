"""预订路径上的四处「失败没说清楚」。

预订是这个项目里后果最重的代码：它替用户做出真实承诺。四条的共同点是
**把一个有后果的中间状态报成了一个无后果的结论**。
"""
from __future__ import annotations

import inspect
import re

import pytest

import booker


def _code(fn) -> str:
    """源码，剥掉注释——断言里引用的坏写法常常也出现在解释性注释里。"""
    return "\n".join(re.sub(r"#.*$", "", ln)
                     for ln in inspect.getsource(fn).split("\n"))


class TestSentinelNeverReachesBooking:
    """`01-01-2050` 是「没有下一个合同起始日」的哨兵，不是入住日。"""

    def test_scraper_nulls_contract_start_date_too(self):
        """两个字段来自**同一个** next_contract_startdate，必须过同一道检查。

        available_from 一直过了，contract_start_date 没有——而 booker 优先用后者。
        """
        import scrapers.holland2stay as h2s

        src = _code(h2s)
        i = src.index("contract_start_date = raw_next")
        assert "is_sentinel_available_from(contract_start_date)" in src[i:i + 600]

    def test_booker_guards_independently(self):
        """库里可能还留着修复之前写进去的行，所以 booker 要自己再挡一次。

        **真调一次**，不 grep 源码：把 ``if`` 改成 ``if False`` 的话名字还在，
        grep 版照样绿——第一版就是那样，变异测试当场漏网。
        """
        assert booker.resolve_start_date("2050-01-01") is None
        assert booker.resolve_start_date("2050-01-01", "2026-10-01") == "2026-10-01"

    def test_expired_dates_are_still_rejected(self):
        """两道判据是**并列**的：哨兵挡「假日期」，>= today 挡「过期日期」。"""
        assert booker.resolve_start_date("2020-01-01") is None

    def test_the_logic_lives_in_one_place(self):
        """这段原先在 booker 里抄了两份，两份都只有 >= today 那一道。

        同一段逻辑写两遍，改的时候一定只改一处——今天这一整轮里这个形状出现了
        四次（badge、隔离分支、pacing、这里）。
        """
        src = _code(booker)
        assert src.count("def resolve_start_date") == 1

        # 判据只允许长在那个函数里。把它整段挖掉，剩下的地方不许再出现。
        # （_code 只剥 # 注释，不剥 docstring，而那个函数的文档里就写着
        #  ``>= today`` ——所以要挖整段，不能数出现次数。）
        fn = _code(booker.resolve_start_date)
        rest = src.replace(fn, "")
        assert ">= today" not in rest, "resolve_start_date 之外又出现了手写的日期判据"
        assert "today_str" not in rest, "第二份的痕迹还在"

    def test_sentinel_year_is_the_judge_not_an_exact_date(self):
        """哨兵换个写法（2099、2050-12-31）时不该原样透出去。"""
        from models import is_sentinel_available_from as sent

        assert sent("2050-01-01") and sent("2050-12-31") and sent("2099-06-01")
        assert not sent("2026-09-02") and not sent("") and not sent(None)


class TestHeldButIncomplete:
    """占房成立之后失败——房在用户名下挂着，不能报成「没抢到」。"""

    def test_phase_exists_and_is_distinct(self):
        from typing import get_args
        phases = get_args(booker.BookingPhase)
        assert "held_incomplete" in phases
        # 不能和这两个混：一个是「没抢到」，一个是「不知道怎么了」，
        # 都会让用户不去处理那套占着的房
        assert "race_lost" in phases and "unknown_error" in phases

    def test_result_carries_the_identifiers(self):
        r = booker.BookingResult(
            listing=None, success=False, message="",
            phase="held_incomplete", held_cart_id="c1", held_order_number="o1")
        assert r.held_cart_id == "c1" and r.held_order_number == "o1"

    def test_exception_carries_what_the_user_needs(self):
        e = booker.BookingHeldButIncomplete(
            cart_id="c1", order_number="", booking_url="u", original="boom")
        assert e.cart_id == "c1" and e.booking_url == "u"

    def test_cart_id_is_recorded_before_the_risky_steps(self):
        """记录必须紧跟 create_booking——它一返回房就占住了。

        记在 place_order 之后的话，中间那几步失败仍然拿不到 cart_id。
        """
        src = _code(booker.try_book)
        i = src.index("create_booking(fetcher, token, sku, start_date)")
        j = src.index("set_payment_method(")
        assert 'held["cart_id"]' in src[i:j], "占房与记录之间插进了别的步骤"

    def test_message_tells_the_user_to_act(self):
        """「预订失败」会让用户以为没抢到，于是既不付款也不取消。"""
        src = _code(booker.try_book)
        i = src.index("held_incomplete")
        block = src[i:i + 2000]
        assert "占在你的账号下" in block or "占住" in block

    def test_does_not_auto_cancel(self):
        """刻意不回滚：取消不可逆，而失败可能只是支付链接超时（房还好好占着）。

        只看 held_incomplete **这个分支自己**——同一个函数里另有一条
        reserved_conflict 的路径会正当地调 cancel_pending_orders，把窗口开大
        就会咬到它（第一版就是这样）。
        """
        src = _code(booker.try_book)
        i = src.index('if held["cart_id"]:')
        j = src.index("BookingHeldButIncomplete(", i)
        assert "cancel_pending_orders" not in src[i:j], (
            "held_incomplete 分支里在自动取消——那是不可逆的")


class TestRentCafeConstructorInsideTry:
    def test_session_is_constructed_inside_try(self):
        """构造在 try 外面的话，异常会穿到 monitor 的 `await future`，
        打断**后续所有用户**的预订结果处理。"""
        from bookers import rentcafe

        src = _code(rentcafe.RentCafeBooker.book) if hasattr(
            rentcafe, "RentCafeBooker") else ""
        whole = _code(rentcafe)
        i = whole.index("RentCafeSession(self._api_key")
        before = whole[:i]
        # 往回找最近的 try / def，try 必须更近
        assert before.rindex("try:") > before.rindex("def "), (
            "RentCafeSession(...) 在 try 之外")


class TestPerUserBookingIsolation:
    def test_await_future_is_guarded(self):
        """一个用户的异常不该带走所有人——这个循环还负责发通知、更新重试队列。"""
        import monitor

        src = _code(monitor._process_booking_results)
        i = src.index("await future")
        window = src[max(0, i - 300):i + 400]
        assert "try:" in window and "except Exception" in window
        assert "continue" in window
