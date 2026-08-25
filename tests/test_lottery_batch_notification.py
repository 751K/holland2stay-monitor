"""H2S 整批放出的抽签房源聚成一条通知，不再一套一封。

起因（2026-08-25 生产）
----------------------
H2S 在一轮里一次性放出 9 套 ``Available in lottery``（同一个 first_seen
15:02:29）。逐条发的结果是同一分钟内 9 封邮件 × 每个匹配用户，当天
``fl1p`` 就是这么在 22 秒内被配额拒发 12 次的——他的过滤条件为空，全收。

为什么只聚合抽签
----------------
进抽签池**不是先到先得**，晚看半小时不影响抽中概率。而「可直接预订」相反：
同日实测中位窗口 154 分钟、最短 4 分钟，那种房源少发一条就是少一次机会。
所以本文件里反复出现的一条断言是**可订房源永远逐条发**——聚合省下来的配额，
不能拿房源机会去换。
"""
from __future__ import annotations

import asyncio
import re

import pytest

import monitor
from models import Listing, is_lottery_status
from monitor import _BATCH_MIN, _split_batchable


def _l(lid: str, status="Available in lottery", source="holland2stay", **kw):
    base = dict(
        id=lid, name=f"House {lid}", status=status, price_raw="€900",
        available_from="2030-01-01", features=["Area: 30 m²", "Type: Studio"],
        url=f"https://h2s/{lid}", city="Eindhoven", source=source,
    )
    base.update(kw)
    return Listing(**base)


# ── 状态判定 ──────────────────────────────────────────────────────────

class TestIsLotteryStatus:
    @pytest.mark.parametrize("status", ["Available in lottery", "To be in lottery"])
    def test_both_h2s_lottery_values(self, status):
        """H2S 的抽签有两个取值（6203 / 6204），漏掉任一个都会让那批房源逃过聚合。"""
        assert is_lottery_status(status)

    @pytest.mark.parametrize("status", [
        "Available to book", "Reserved", "Occupied", "", None, "Not available",
    ])
    def test_non_lottery(self, status):
        assert not is_lottery_status(status)

    def test_case_and_space_insensitive(self):
        assert is_lottery_status("  AVAILABLE IN LOTTERY  ")


# ── 拆分 ──────────────────────────────────────────────────────────────

class TestSplit:
    def test_batches_h2s_lottery(self):
        ms = [_l("a"), _l("b"), _l("c")]
        batched, singles = _split_batchable(ms)
        assert [x.id for x in batched] == ["a", "b", "c"]
        assert singles == []

    def test_bookable_never_batched(self):
        """这是本文件的重点断言：可订房源逐条发，一套都不许进聚合。"""
        ms = [_l("a"), _l("b"), _l("hot", status="Available to book")]
        batched, singles = _split_batchable(ms)
        assert [x.id for x in batched] == ["a", "b"]
        assert [x.id for x in singles] == ["hot"]

    def test_other_sources_never_batched(self):
        """别的平台没有抽签状态，就算状态里带 lottery 也不该走这条路。"""
        ms = [_l("a"), _l("b"), _l("x", source="xior"), _l("o", source="ourdomain")]
        batched, singles = _split_batchable(ms)
        assert [x.id for x in batched] == ["a", "b"]
        assert {x.id for x in singles} == {"x", "o"}

    def test_single_lottery_stays_single(self):
        """1 套时聚合没有收益，反而丢掉类型/楼层/能耗那几行。"""
        ms = [_l("only")]
        batched, singles = _split_batchable(ms)
        assert batched == []
        assert [x.id for x in singles] == ["only"]

    def test_threshold_is_two(self):
        assert _BATCH_MIN == 2
        assert _split_batchable([_l("a"), _l("b")])[0] != []

    def test_below_threshold_keeps_original_order(self):
        """不够数退回逐条时，别把那套抽签甩到队尾——日志和消息都按这个顺序走。"""
        ms = [_l("lot"), _l("hot", status="Available to book")]
        batched, singles = _split_batchable(ms)
        assert batched == []
        assert [x.id for x in singles] == ["lot", "hot"]

    def test_empty(self):
        assert _split_batchable([]) == ([], [])

    def test_to_be_in_lottery_also_batches(self):
        ms = [_l("a", status="To be in lottery"), _l("b", status="To be in lottery")]
        assert len(_split_batchable(ms)[0]) == 2


# ── 消息文案 ──────────────────────────────────────────────────────────

class TestBatchMessage:
    def _fmt(self, listings, lang="zh"):
        from notifier import _format_new_batch
        return _format_new_batch(listings, lang=lang)

    def test_says_how_many(self):
        txt = self._fmt([_l(str(i)) for i in range(9)])
        assert "× 9" in txt, txt

    def test_every_listing_has_its_own_link(self):
        """聚合不能让链接消失——用户就是靠它点进去的。"""
        ms = [_l("a"), _l("b"), _l("c")]
        txt = self._fmt(ms)
        for l in ms:
            assert l.url in txt
            assert l.name in txt

    def test_long_batch_is_capped_but_reports_the_total(self):
        """列全会被 Telegram 的 4096 字符上限截断，而截断位置不可控。"""
        from notifier import _BATCH_LIST_LIMIT
        ms = [_l(str(i)) for i in range(_BATCH_LIST_LIMIT + 7)]
        txt = self._fmt(ms)
        assert txt.count("https://h2s/") == _BATCH_LIST_LIMIT
        assert f"× {_BATCH_LIST_LIMIT + 7}" in txt   # 抬头仍报真实总数
        # 「余下 7 套」必须整句匹配。写成 `"7" in txt` 是没用的——抬头的
        # 「× 27」里就有个 7，删掉整行余量提示照样绿（变异 M12 就是这么活下来的）。
        assert re.search(r"另有\s*7\s*套", txt), txt

    def test_no_cap_notice_when_under_limit(self):
        assert "另有" not in self._fmt([_l("a"), _l("b")])

    def test_localised(self):
        assert "抽签房源" in self._fmt([_l("a"), _l("b")], lang="zh")
        assert "Lottery listings" in self._fmt([_l("a"), _l("b")], lang="en")

    def test_fits_telegram_single_message(self):
        """4096 是 Telegram 单条上限，超了会被切成两条或直接失败。"""
        from notifier import _BATCH_LIST_LIMIT
        ms = [_l(f"listing-with-a-fairly-long-slug-{i}") for i in range(_BATCH_LIST_LIMIT)]
        assert len(self._fmt(ms)) < 4096


class TestNotifierMethod:
    def test_sends_exactly_one_message(self):
        """整个改动的目的就是这一条：9 套房源 → 1 次投递。"""
        from notifier import BaseNotifier

        sent: list[str] = []

        class _N(BaseNotifier):
            async def _send(self, text: str) -> bool:
                sent.append(text)
                return True

            async def close(self) -> None:
                return None

        n = _N(language="zh")
        ok = asyncio.run(n.send_new_listings_batch([_l(str(i)) for i in range(9)]))
        assert ok is True
        assert len(sent) == 1, f"发了 {len(sent)} 条，聚合没生效"
        assert "× 9" in sent[0]

    def test_empty_sends_nothing(self):
        from notifier import BaseNotifier

        sent: list[str] = []

        class _N(BaseNotifier):
            async def _send(self, text: str) -> bool:
                sent.append(text)
                return True

            async def close(self) -> None:
                return None

        assert asyncio.run(_N().send_new_listings_batch([])) is False
        assert sent == []


# ── 接线 ──────────────────────────────────────────────────────────────

class TestWiredIntoNotifyNewListings:
    """走 ``_notify_new_listings`` 全链路。

    上面那些用例都直接调 ``_split_batchable`` / ``send_new_listings_batch``，
    接线断了一条都不会红——本项目在同类的跨函数交接上栽过两次（Xior 的
    ``building_key=``、pacing 的 ``relax()``）。所以这里必须从入口打进去。
    """

    class _Push:
        @staticmethod
        def get_client(): return None
        @staticmethod
        def get_fcm_client(): return None
        @staticmethod
        def should_aggregate(n): return False

    class _AllPass:
        class listing_filter:
            @staticmethod
            def is_empty(): return True
            @staticmethod
            def passes(listing): return True
        id = "u1"
        name = "Everyone"

    class _Spy:
        def __init__(self):
            self.singles = []
            self.batches = []

        async def send_new_listing(self, listing):
            self.singles.append(listing.id)
            return True

        async def send_new_listings_batch(self, listings):
            self.batches.append([l.id for l in listings])
            return True

    def _run(self, listings, storage):
        spy = self._Spy()
        asyncio.run(monitor._notify_new_listings(
            listings, [(self._AllPass(), spy)], None, storage, self._Push,
        ))
        return spy

    def test_nine_lottery_listings_become_one_message(self, temp_db):
        """重放 2026-08-25 15:02:29 那一轮：9 套同时进库。"""
        ms = [_l(f"lot{i}") for i in range(9)]
        temp_db.diff(ms)
        spy = self._run(ms, temp_db)

        assert len(spy.batches) == 1, f"没聚合: batches={spy.batches}"
        assert len(spy.batches[0]) == 9
        assert spy.singles == [], f"还有 {len(spy.singles)} 条在逐条发"

    def test_bookable_still_goes_out_one_by_one(self, temp_db):
        """同一轮里混着可订房源时，它必须单独发——那种房源等不起。"""
        ms = [_l("lot1"), _l("lot2"), _l("hot", status="Available to book")]
        temp_db.diff(ms)
        spy = self._run(ms, temp_db)

        assert spy.batches == [["lot1", "lot2"]]
        assert spy.singles == ["hot"]

    def test_single_lottery_still_uses_the_normal_path(self, temp_db):
        ms = [_l("only")]
        temp_db.diff(ms)
        spy = self._run(ms, temp_db)
        assert spy.batches == []
        assert spy.singles == ["only"]

    def test_all_listings_still_get_marked(self, temp_db):
        """聚合不能让 notified 标记漏掉——漏了会被重放当成未投递事件反复重发。"""
        ms = [_l(f"lot{i}") for i in range(9)]
        temp_db.diff(ms)
        assert len(temp_db.pending_new_listings()) == 9
        self._run(ms, temp_db)
        assert temp_db.pending_new_listings() == []

    def test_filtered_out_listings_never_reach_the_batch(self, temp_db):
        """过滤条件先于聚合生效，别让摘要变成绕过筛选的后门。"""
        class _OnlyLot3:
            class listing_filter:
                @staticmethod
                def is_empty(): return False
                @staticmethod
                def passes(listing): return listing.id in ("lot3", "lot4")
            id = "u2"
            name = "Picky"

        ms = [_l(f"lot{i}") for i in range(9)]
        temp_db.diff(ms)
        spy = self._Spy()
        asyncio.run(monitor._notify_new_listings(
            ms, [(_OnlyLot3(), spy)], None, temp_db, self._Push,
        ))
        assert spy.batches == [["lot3", "lot4"]]
        assert spy.singles == []
