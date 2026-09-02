"""通知交付语义：从 at-most-once 改成 at-least-once。

问题
----
``run_once`` 的顺序是::

    storage.diff(fresh)          ← 提交新状态到库（含 last_status）
       ↓  ...51 个用户 × N 条房源的网络往返...
    notifier.send_*()
       ↓
    mark_notified_batch()

``diff()`` **检测变更的副作用就是覆盖掉用来检测的那个旧状态**。中间任何中断——
崩溃、部署、OOM——那批事件永久丢失：下一轮 ``diff()`` 看到
``old_status == new_status``，不产出任何事件。

而 ``notified`` 字段看起来像一本 at-least-once 的账，实际是**只写的**：全仓库
没有任何 ``SELECT ... WHERE notified=0``，只有两条 ``UPDATE ... SET notified=1``。

触发条件很日常：2026-08-20 一天之内部署了 12 次，每次 ``--force-recreate`` 都在
打断正在跑的轮次。

为什么不能直接重放 notified=0
-----------------------------
上线前生产实测：**559 条 listings 里 403 条是 notified=0**，319 条 status_changes
里 204 条是 0。它们绝大多数不是「丢失的事件」，而是「没有任何用户的筛选条件匹配
到它」——旧代码只在 ``notified_this``（至少投递给一个用户）时才标记。

直接重放 = 给 51 个真实用户一次性轰 403 条房源。所以这次改动包含三件事，缺一
不可：

1. **语义修正**：``notified=1`` 改成「已走完通知阶段」，而不是「至少投递成功」。
   否则 0 池会立刻重新堆积，重放信号毫无意义。
2. **一次性迁移**：把存量 notified=0 全部置 1。它们是历史数据，重放是错的。
3. **有界重放**：只重放时间窗内的，且单轮有条数上限。
"""
from __future__ import annotations

import json

import pytest

from models import Listing
from storage import Storage


def _ago(**kw) -> str:
    """生产格式的「N 小时前」。

    **必须与 mstorage._listings._now_iso() 同格式**——用 SQLite 的
    ``datetime('now', ...)`` 构造会和查询里的格式凑巧一致，把字符串比较的 bug
    整个盖住（这个文件原先就是那么写的）。
    """
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


def _l(lid="L1", status="Available to book", features=None):
    return Listing(
        id=lid, name=lid, status=status, price_raw="€1000",
        available_from="", url="", city="Eindhoven", source="holland2stay",
        features=list(features or []),
    )


@pytest.fixture
def st(tmp_path):
    s = Storage(tmp_path / "t.db", timezone_str="UTC")
    yield s
    s.close()


class TestPendingIsReadable:
    """``notified=0`` 必须真的有人读——否则它只是个没人对账的账本。"""

    def test_new_listing_is_pending_until_marked(self, st):
        st.diff([_l("L1")])
        assert [l.id for l in st.pending_new_listings()] == ["L1"]

        st.mark_notified_batch(["L1"])
        assert st.pending_new_listings() == []

    def test_pending_rebuilds_a_usable_listing(self, st):
        """重放出来的必须是能直接喂给 notifier 的 Listing，不是半成品 row。

        通知模板要读 name / status / price / url / features，缺一条就会在
        重放路径上炸——而那条路径平时不跑，炸了也没人知道。
        """
        st.diff([_l("L1", features=["Building: The Wall", "Type: Studio"])])
        got = st.pending_new_listings()[0]
        assert isinstance(got, Listing)
        assert got.id == "L1"
        assert got.status == "Available to book"
        assert got.city == "Eindhoven"
        assert got.source == "holland2stay"
        assert "Building: The Wall" in got.features

    def test_status_change_is_pending_until_marked(self, st):
        st.diff([_l("L1", status="Occupied")])
        st.mark_notified_batch(["L1"])
        st.diff([_l("L1", status="Available to book")])

        pend = st.pending_status_changes()
        assert [(l.id, o, n) for l, o, n in pend] == [
            ("L1", "Occupied", "Available to book")
        ]
        st.mark_status_change_notified_batch(["L1"])
        assert st.pending_status_changes() == []


class TestCrashBetweenDiffAndNotify:
    """核心场景：diff() 已提交、通知还没发出去，进程就死了。"""

    def test_the_event_survives(self, st):
        # 第 1 轮：diff 提交了，通知阶段还没跑完就被 SIGKILL
        st.diff([_l("L1")])

        # 第 2 轮：diff 本身已经产不出任何事件了——状态早就写进库了
        new, changes = st.diff([_l("L1")])
        assert new == [] and changes == [], "前提没成立：diff 仍在重复产出"

        # 但重放能捞回来
        assert [l.id for l in st.pending_new_listings()] == ["L1"], (
            "diff 已经把旧状态覆盖掉了，这条事件永久丢失"
        )

    def test_status_change_survives_too(self, st):
        st.diff([_l("L1", status="Occupied")])
        st.mark_notified_batch(["L1"])
        st.diff([_l("L1", status="Available to book")])   # 崩在这之后

        st.diff([_l("L1", status="Available to book")])   # 下一轮：无事件
        assert len(st.pending_status_changes()) == 1


class TestNoNotificationStorm:
    """三道闸，任何一道漏掉都会给 51 个真实用户造成一次轰炸。"""

    def test_processed_but_undelivered_is_still_marked(self, st):
        """闸①语义：**没有任何用户匹配**的房源，也算走完了通知阶段。

        旧语义只在「至少投递给一个用户」时标记，于是无人匹配的房源永远停在 0。
        生产实测 559 条里有 403 条就是这么来的。不修这条，0 池会立刻重新堆积，
        重放信号退化成噪音。
        """
        st.diff([_l("L1")])
        # 通知阶段跑完了，但没有任何用户匹配 → 仍然要标记
        st.mark_notified_batch(["L1"])
        assert st.pending_new_listings() == []

    def test_migration_clears_the_legacy_backlog(self, tmp_path):
        """闸②迁移：存量 notified=0 一次性置 1，绝不重放。"""
        db = tmp_path / "legacy.db"
        s1 = Storage(db, timezone_str="UTC")
        s1.diff([_l(f"OLD{i}") for i in range(50)])
        assert len(s1.pending_new_listings(limit=1000)) == 50
        s1.close()

        # 模拟「本次升级第一次打开这个库」
        Storage._reset_backlog_migration_for_tests(db)
        s2 = Storage(db, timezone_str="UTC")
        try:
            assert s2.pending_new_listings(limit=1000) == [], (
                "存量积压没被迁移掉——上线瞬间会把 400 多条历史房源重新推给用户"
            )
        finally:
            s2.close()

    def test_replay_has_a_time_window(self, st):
        """闸③时间窗：太老的不重放。房子早没了，推过去只是打扰。

        ⚠️ 时间戳必须用**生产写入的那个格式**（``_now_iso()`` 的带时区 ISO）。
        这条用例原先写 ``datetime('now','-3 hours')``——SQLite 自己的空格分隔格式，
        和查询里 ``datetime('now','-N minutes')`` 的格式一致，字符串比较恰好正确。
        于是它绿着，而**生产里那个 90 分钟的窗口一直退化成「当天 UTC 零点起」**：
        第 10 位 ``T``(0x54) 对空格(0x20)，同一 UTC 日期内恒为真。

        用 _ago() 构造就把这层伪装拆掉了。
        """
        st.diff([_l("L1")])
        st.conn.execute(
            "UPDATE listings SET first_seen = ? WHERE id='L1'", (_ago(hours=3),)
        )
        st.conn.commit()
        assert st.pending_new_listings(within_minutes=60) == []
        assert len(st.pending_new_listings(within_minutes=600)) == 1

    def test_window_is_not_a_string_comparison(self, st):
        """同一 UTC 日期内的旧行必须被挡住——这正是 bug 的形状。

        取 9 小时前而不是 25 小时前：跨天的那种字符串比较也能挡住，只有当天的
        才暴露问题。实测就是「9 小时前仍判为窗口内」。
        """
        st.diff([_l("L1")])
        st.conn.execute(
            "UPDATE listings SET first_seen = ? WHERE id='L1'", (_ago(hours=9),)
        )
        st.conn.commit()
        assert st.pending_new_listings(within_minutes=90) == [], (
            "9 小时前的行落进了 90 分钟的窗口——比较退化成了字符串比较")

    def test_status_change_window_too(self, st):
        """状态变更那条查询是同一个形状，也要挡住。"""
        st.diff([_l("L1", status="Available to book")])
        st.diff([_l("L1", status="Occupied")])
        assert len(st.pending_status_changes(within_minutes=90)) == 1
        st.conn.execute("UPDATE status_changes SET changed_at = ?", (_ago(hours=9),))
        st.conn.commit()
        assert st.pending_status_changes(within_minutes=90) == []

    def test_replay_has_a_batch_cap(self, st):
        """闸③条数：单轮重放有上限，异常情况下不会一次性炸开。"""
        st.diff([_l(f"L{i}") for i in range(30)])
        assert len(st.pending_new_listings(limit=5)) == 5

    def test_stale_pending_is_retired_not_resent_forever(self, st):
        """超窗的积压要能被「归档」，否则 0 池只增不减，每轮白查一次。"""
        st.diff([_l("L1")])
        st.conn.execute(
            "UPDATE listings SET first_seen = ? WHERE id='L1'", (_ago(hours=3),)
        )
        st.conn.commit()
        n = st.retire_stale_pending(within_minutes=60)
        assert n == 1
        assert st.conn.execute(
            "SELECT notified FROM listings WHERE id='L1'"
        ).fetchone()[0] == 1


class TestDeliveryIsAtLeastOnceNotExactlyOnce:
    def test_a_replayed_event_can_be_sent_twice(self, st):
        """明确记下这个取舍：崩溃时机不巧的话用户可能收到两次。

        重复通知只是打扰，漏掉通知会让人错过房子——这是个不对称的代价，
        所以选 at-least-once。
        """
        st.diff([_l("L1")])
        first = st.pending_new_listings()
        assert len(first) == 1
        # 假设通知实际发出去了，但进程在 mark 之前死了
        assert len(st.pending_new_listings()) == 1, "会重发一次，这是有意的"


class TestShadowSourcesStayShadowed:
    """影子 source 的房源**必须**在丢弃时就标记成已处理。

    影子机制的原始注释写着：「副作用是这些 listing 的 notified 一直是 0。
    取消影子后不会补发历史——这是想要的：解除影子不该给用户灌一堆积压通知。」

    那个副作用在 ``notified`` 只写不读的年代是无害的。**加上重放之后它会直接
    翻车**：被静默拦下的房源停在 0，下一轮重放原样捞出来推给用户，影子 source
    的整个保证当场失效。

    所以「丢弃」也是通知阶段的一种结论——决定了「不发」，就该标记为已处理。
    """

    def test_dropped_shadow_listings_are_marked_processed(self, st):
        import monitor

        class _Cfg:
            shadow_sources = ("ourcampus",)

        shadow = _l("S1")
        shadow.source = "ourcampus"
        normal = _l("N1")
        st.diff([shadow, normal])

        kept_new, kept_sc = monitor._drop_shadow_sources(
            _Cfg(), [shadow, normal], [], storage=st,
        )
        assert [l.id for l in kept_new] == ["N1"]
        assert [l.id for l in st.pending_new_listings()] == ["N1"], (
            "影子房源没被标记，重放会把它推给用户——影子保证当场失效"
        )

    def test_no_shadow_configured_is_a_no_op(self, st):
        import monitor

        class _Cfg:
            shadow_sources = ()

        st.diff([_l("L1")])
        kept_new, _ = monitor._drop_shadow_sources(_Cfg(), [_l("L1")], [], storage=st)
        assert [l.id for l in kept_new] == ["L1"]
        assert len(st.pending_new_listings()) == 1


class TestMergePendingIntoTheRound:
    """``_merge_pending_events`` 把重放事件并进本轮的两条列表。"""

    def test_merges_and_reports_count(self, st):
        import monitor

        st.diff([_l("L1")])            # 上一轮崩了，没标记
        new, sc = [], []
        n = monitor._merge_pending_events(st, new, sc)
        assert n == 1
        assert [l.id for l in new] == ["L1"]

    def test_does_not_duplicate_what_this_round_already_found(self, st):
        """同一条房源刚上架又立刻变状态时会撞上——不能推两遍。"""
        import monitor

        st.diff([_l("L1")])
        this_round = [_l("L1")]
        n = monitor._merge_pending_events(st, this_round, [])
        assert n == 0
        assert len(this_round) == 1

    def test_mutates_in_place(self, st):
        """就地改：调用方后面还要把这两个列表喂给通知和预订两条路。"""
        import monitor

        st.diff([_l("L1")])
        new = []
        monitor._merge_pending_events(st, new, [])
        assert new, "没有就地修改，调用方拿到的还是空列表"

    def test_replayed_events_get_marked_and_stop_replaying(self, st):
        """重放出来的事件走完通知阶段后要被标记，否则会每轮无限重放。"""
        import monitor

        st.diff([_l("L1")])
        new = []
        monitor._merge_pending_events(st, new, [])
        st.mark_notified_batch([l.id for l in new])

        again = []
        assert monitor._merge_pending_events(st, again, []) == 0


class TestNotifyPhaseMarksEverythingItProcessed:
    """``notified=1`` 的语义是「已处理」，不是「已送达」。

    这是三道闸里最容易被改回去的一道：旧写法 ``if notified_this`` 读起来非常
    自然（「通知成功了才算通知过」），但它会让**无人匹配**的房源永远停在 0。
    生产实测 559 条 listings 里 403 条就是这么来的，而重放读的正是这个字段——
    信号会被这批噪音彻底淹没。
    """

    @staticmethod
    class _Push:
        @staticmethod
        def get_client(): return None
        @staticmethod
        def get_fcm_client(): return None
        @staticmethod
        def should_aggregate(n): return False

    class _RejectAll:
        """一个谁都不匹配的用户：filter 非空且永远 False。"""
        class listing_filter:
            @staticmethod
            def is_empty(): return False
            @staticmethod
            def passes(listing): return False
        id = "u1"
        name = "Nobody"

    def test_listing_matching_nobody_is_still_marked(self, st):
        import asyncio

        import monitor

        st.diff([_l("L1")])
        assert len(st.pending_new_listings()) == 1

        asyncio.run(monitor._notify_new_listings(
            [_l("L1")],
            [(self._RejectAll(), object())],   # notifier 不会被调到
            None, st, self._Push,
        ))
        assert st.pending_new_listings() == [], (
            "没有任何用户匹配的房源没被标记——0 池会重新堆积，"
            "重放信号被噪音淹没"
        )

    def test_listing_whose_delivery_failed_is_also_marked(self, st):
        """投递失败同样算处理过。

        渠道坏了要靠渠道自己的重试/告警去修，不该让它把这条事件永远钉在重放
        队列里——那会变成每轮都朝一个坏渠道重发。
        """
        import asyncio

        import monitor

        class _AllPass:
            class listing_filter:
                @staticmethod
                def is_empty(): return True
                @staticmethod
                def passes(listing): return True
            id = "u2"
            name = "Someone"

        class _DeadNotifier:
            async def send_new_listing(self, listing): return False

        st.diff([_l("L1")])
        asyncio.run(monitor._notify_new_listings(
            [_l("L1")], [(_AllPass(), _DeadNotifier())], None, st, self._Push,
        ))
        assert st.pending_new_listings() == []

    def test_status_change_matching_nobody_is_still_marked(self, st):
        """状态变更侧要有完全对称的行为——两条路各写各的，很容易只改一半。"""
        import asyncio

        import monitor

        st.diff([_l("L1", status="Occupied")])
        st.mark_notified_batch(["L1"])
        st.diff([_l("L1", status="Available to book")])
        assert len(st.pending_status_changes()) == 1

        asyncio.run(monitor._notify_status_changes(
            [(_l("L1", status="Available to book"), "Occupied", "Available to book")],
            [(self._RejectAll(), object())],
            None, st, self._Push,
        ))
        assert st.pending_status_changes() == [], (
            "无人匹配的状态变更没被标记——和新房源那侧只改了一半"
        )
