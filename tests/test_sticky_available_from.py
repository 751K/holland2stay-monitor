"""
入住日期不该被「这一轮没抓到」冲掉。

H2S 的房源在「可订」阶段有真实的 available_from。一旦转成 Reserved，上游的
next_contract_startdate 就变成 2050-01-01 哨兵；scrapers/holland2stay.py 认出
哨兵后返回 None，而 diff() 原来无条件写回库，于是那个真日期被 None 冲掉，界面
上只剩一个「—」。

2026-08-28 线上实测：H2S 445 条里 76 条没有日期，Reserved 47 条中占了 45 条，
而 Available to book 的 19 条一条都不缺。23 条能从 status_changes 证明它们曾经
是「可订」——那个日期确实存在过，是被我们自己删掉的。

判据是「新值为空才保留旧值」。上游给了新日期就用新的：空值的含义是「这一轮
没拿到」，不是「这个房子没有入住日」。
"""
from __future__ import annotations

import pytest

from models import Listing, is_sentinel_available_from
from mstorage._listings import _sticky_available_from


def _l(lid="l1", *, status="Available to book", avail=None, source="holland2stay"):
    return Listing(
        id=lid, name="Test flat", status=status,
        price_raw="€900", available_from=avail,
        features=[], url=f"https://x/{lid}", city="Eindhoven", source=source,
    )


# ── 合并函数本身 ────────────────────────────────────────────────

class TestMergeRule:
    @pytest.mark.parametrize("fresh,old,want", [
        # 新值有就用新的——修正、重新放盘都要覆盖得了
        ("2026-09-01", "2026-06-01", "2026-09-01"),
        ("2026-09-01", None,         "2026-09-01"),
        # 新值为空 → 保留旧的，这是这次修的那条
        (None,         "2026-06-01", "2026-06-01"),
        ("",           "2026-06-01", "2026-06-01"),
        ("   ",        "2026-06-01", "2026-06-01"),
        # 两边都空 → None，别把空串写进库
        (None,         None,         None),
        ("",           "",           None),
        # 哨兵两侧都按「空」处理
        ("2050-01-01", "2026-06-01", "2026-06-01"),   # 新值是哨兵 → 保住旧的真值
        ("2099-12-31", "2026-06-01", "2026-06-01"),   # 换个写法照样认
        ("2026-10-15", "2050-01-01", "2026-10-15"),   # 旧值是哨兵 → 新的真值赢
        (None,         "2050-01-01", None),           # 旧值是哨兵 → 不保留
        ("2050-01-01", "2050-01-01", None),
    ])
    def test_rule(self, fresh, old, want):
        assert _sticky_available_from(fresh, old) == want

    def test_sentinel_is_never_locked_in(self, temp_db):
        """哨兵不能靠粘性住进库里。

        scrapers 层已经认过一次哨兵，这里是落库前的最后一道。少了它，抓取层的
        过滤哪天回退，哨兵就会被粘性当成「已知」永远锁住——正好是这个函数本来
        要防的事情的反面。
        """
        st = temp_db
        st.diff([_l(avail="2026-09-01")])
        # 抓取层漏了一个哨兵进来
        st.diff([_l(status="Reserved", avail="2050-01-01")])
        assert st.get_all_listings()[0]["available_from"] == "2026-09-01"

    def test_scraper_and_storage_share_one_criterion(self):
        """两处各写一个 2050 会分叉，而分叉的表现是一个假日期悄悄进了库。"""
        import inspect

        import scrapers.holland2stay as h2s
        import mstorage._listings as li

        for mod in (h2s, li):
            src = inspect.getsource(mod)
            assert "is_sentinel_available_from" in src, mod.__name__
            assert ">= 2050" not in src, f"{mod.__name__} 里还留着手写的 2050"
        assert is_sentinel_available_from("2050-01-01")
        assert not is_sentinel_available_from("2026-09-01")


# ── 真实场景：可订 → Reserved ────────────────────────────────────

class TestBookableToReserved:
    def test_date_survives_the_transition(self, temp_db):
        """就是用户报的那个 bug。"""
        st = temp_db
        st.diff([_l(avail="2026-09-01")])
        assert st.get_all_listings()[0]["available_from"] == "2026-09-01"

        # 转 Reserved，上游给哨兵 → scraper 已经把它变成 None
        st.diff([_l(status="Reserved", avail=None)])

        row = st.get_all_listings()[0]
        assert row["status"] == "Reserved"
        assert row["available_from"] == "2026-09-01", "入住日期被冲掉了"

    def test_in_memory_listing_is_synced(self, temp_db):
        """diff() 返回的对象要和库里一致。

        monitor 拿这批对象去发通知（notifier.py 读 listing.available_from）。
        不同步就会出现「网页上有日期、推送里是 ?」——同一件事两个说法。
        """
        st = temp_db
        st.diff([_l(avail="2026-09-01")])

        fresh = _l(status="Reserved", avail=None)
        _, changes = st.diff([fresh])

        assert changes, "状态没变？前置条件不成立"
        assert fresh.available_from == "2026-09-01"
        assert changes[0][0].available_from == "2026-09-01"

    def test_upstream_correction_wins(self, temp_db):
        """上游改了日期就得听上游的——粘性只针对空值。"""
        st = temp_db
        st.diff([_l(avail="2026-09-01")])
        st.diff([_l(avail="2026-10-15")])
        assert st.get_all_listings()[0]["available_from"] == "2026-10-15"

    def test_new_listing_with_no_date_stays_empty(self, temp_db):
        """第一次就没日期的，不该凭空长出一个。"""
        st = temp_db
        st.diff([_l(avail=None)])
        assert not st.get_all_listings()[0]["available_from"]

    def test_survives_repeated_empty_rounds(self, temp_db):
        """连续多轮抓不到也要一直保住——Reserved 会持续很多轮。"""
        st = temp_db
        st.diff([_l(avail="2026-09-01")])
        for _ in range(5):
            st.diff([_l(status="Reserved", avail=None)])
        assert st.get_all_listings()[0]["available_from"] == "2026-09-01"


# ── 另一条 UPDATE 分支 ──────────────────────────────────────────

class TestHoldBranch:
    """diff() 里有两条 UPDATE：正常那条，和 booking-hold / 未通过权威校验时
    走的那条。两条都写 available_from，只修一条等于没修。"""

    def test_booking_hold_branch_also_keeps_the_date(self, temp_db, monkeypatch):
        st = temp_db
        st.diff([_l(avail="2026-09-01")])

        monkeypatch.setattr(type(st), "_should_keep_booking_hold",
                            lambda self, old, new, now: True)
        st.diff([_l(status="Reserved", avail=None)])

        assert st.get_all_listings()[0]["available_from"] == "2026-09-01"


# ── 平台无关 ────────────────────────────────────────────────────

def test_applies_to_every_platform(temp_db):
    """今天只有 H2S 会产生空值（其余三个线上 0 条），但「未知不该覆盖已知」
    和平台无关。写成 H2S 专属是在赌上游行为不变。"""
    st = temp_db
    for src in ("holland2stay", "xior", "ourdomain", "ourcampus"):
        st.diff([_l(lid=f"x-{src}", avail="2026-09-01", source=src)])
        st.diff([_l(lid=f"x-{src}", status="Reserved", avail=None, source=src)])

    rows = {r["id"]: r for r in st.get_all_listings()}
    for src in ("holland2stay", "xior", "ourdomain", "ourcampus"):
        assert rows[f"x-{src}"]["available_from"] == "2026-09-01", src
