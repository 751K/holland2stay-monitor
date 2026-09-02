"""prewarm 撞上 Cloudflare 403 时，``_submit_bookings`` 必须开抑制窗口而不是崩。

这条分支曾经引用一个不存在的名字
--------------------------------
``monitor.py`` 里写的是 ``_mark_h2s_login_blocked(e, storage)``，而
``_submit_bookings`` 没有 ``storage`` 参数、模块里也没有同名全局。**分支一执行就
NameError**——异常穿透 run_once，本轮通知全丢，而抑制窗口一秒都没开：既没拦住后续
对登录链路的骚扰，也没给 WAF 降温的时间。

它长期没被发现，是因为上面挨着的那条注释说的正是「这条分支从未执行过」——原先
except 的是一个没人 raise 的 ``BookingBlockedError``。**修好类型之后它才第一次真的
跑到这里，而 NameError 就在那儿等着。** 一个 bug 被另一个 bug 挡住了。

同款代码在 run_once 内另有两处（都正确传了 storage），已有测试只覆盖那两处——
所以这里要**真调一次**，不能靠 grep 源码。
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

import monitor
from scrapers.base import BlockedError


class _AutoBook:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled


class _User:
    def __init__(self, uid: str = "u1"):
        self.id = uid
        self.name = "tester"
        self.auto_book = _AutoBook()


class _Notifier:
    pass


class _Listing:
    """候选只要能被 sorted(key=...) 排到即可。"""

    def __init__(self, lid: str = "L1"):
        self.id = lid
        self.name = "Somewhere 1"
        self.features = ["Area: 30 m²"]
        self.source = "holland2stay"
        self.status = "Available to book"
        self.price_raw = "€900"
        self.url = ""
        self.city = "Eindhoven"


@pytest.fixture
def blocked_future():
    """一个**已完成**且抛 BlockedError 的 future——正是 CF 403 的形状。"""
    loop = asyncio.new_event_loop()
    try:
        fut = loop.create_future()
        fut.set_exception(BlockedError("Cloudflare 403 during prewarm"))
        # 取走异常，避免 GC 时告警；set_exception 之后 done() 已为 True
        yield loop, fut
    finally:
        loop.close()


def _call(loop, fut, storage=None):
    return monitor._submit_bookings(
        loop,
        {"u1": [_Listing()]},
        [(_User(), _Notifier())],
        {},                      # prewarm_cached：不命中，走 futures
        {"u1": fut},
        {},                      # status_transition
        loop.time() + 30,        # booking_deadline
        storage=storage,
    )


class TestSignature:
    def test_storage_is_a_real_parameter(self):
        """不是参数的话，函数体里那个 `storage` 就是模块全局——而它不存在。"""
        params = inspect.signature(monitor._submit_bookings).parameters
        assert "storage" in params

    def test_module_has_no_storage_global(self):
        """钉住「不能靠全局兜底」这个前提。

        哪天真加了模块级 storage，这条会红——那时 NameError 消失了，但
        _submit_bookings 会悄悄用上别处的 storage，是另一种错。
        """
        assert not hasattr(monitor, "storage")

    def test_call_site_passes_it(self):
        src = inspect.getsource(monitor.run_once)
        i = src.index("_submit_bookings(")
        assert "storage=storage" in src[i:i + 400], "调用点没把 storage 传进去"


class TestBehaviour:
    def test_does_not_raise(self, blocked_future):
        """核心断言：这条分支跑得完。NameError 会让整轮通知一起没了。"""
        loop, fut = blocked_future
        assert monitor._h2s_login_suppressed_remaining() == 0
        _call(loop, fut)                      # 不抛即通过

    def test_opens_the_suppression_window(self, blocked_future):
        """开窗口才是这条分支存在的理由——403 之后继续碰登录只会让 WAF 更热。"""
        loop, fut = blocked_future
        _call(loop, fut)
        assert monitor._h2s_login_suppressed_remaining() > 0

    def test_skips_booking_for_that_user(self, blocked_future):
        """窗口一开，本轮就不该再给这个用户提交预订。"""
        loop, fut = blocked_future
        assert _call(loop, fut) == []

    def test_persists_when_storage_is_given(self, blocked_future, tmp_path):
        """给了 storage 就要落库——否则抑制窗口活不过一次重启。

        这正是当初写 `_mark_h2s_login_blocked(e, storage)` 的用意；改成不传
        storage 只会退化成进程内抑制，而调用点明明拿得到。
        """
        from storage import Storage

        loop, fut = blocked_future
        st = Storage(tmp_path / "t.db")
        key = monitor._h2s_login_block._key_until      # backoff:h2s_login_block:until
        assert st.get_meta(key, "") in ("", "0", "0.0")

        _call(loop, fut, storage=st)

        written = st.get_meta(key, "")
        assert written not in ("", "0", "0.0"), "抑制窗口没落库，重启后就丢了"
        assert float(written) > 0
