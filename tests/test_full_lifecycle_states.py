"""feed 覆盖了「已预留」时，「从 feed 消失」的含义就变了。

H2S 的 ``available_to_book`` 有六个取值，我们原先只抓两个（可订 179、抽签 336）。
那种配置下「消失」是**有歧义**的——可能被人下单了（Reserved 6203），也可能彻底
没了（Niet beschikbaar 180），所以先推 Reserved 留出 2 小时付款窗口，再判终态。

把 Reserved 也抓进来之后，消失就没有歧义了：它已经掉出我们跟踪的全部状态。此时
再推一次 Reserved，是凭空造一个平台从没说过的状态，还会把 ``status_is_inferred=1``
打在一条本可以如实上报的房源上。

其余三个平台的 feed 只列可订单元，没有等价的「已预留」可抓，判据不变。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from models import Listing
from mstorage import Storage


def _iso(**delta):
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


@pytest.fixture
def db(tmp_path):
    st = Storage(tmp_path / "listings.db")
    yield st
    st.close()


def _add(st, lid, source, status="Available to book", hours_ago=5.0):
    st.diff([Listing(id=lid, name=lid, status=status, price_raw="1000",
                     available_from="", url="", features=[],
                     city="Eindhoven", source=source)])
    st.conn.execute("UPDATE listings SET last_seen=? WHERE id=?",
                    (_iso(hours=hours_ago), lid))
    st.conn.commit()


def _row(st, lid):
    return dict(st.conn.execute(
        "SELECT status, status_is_inferred FROM listings WHERE id=?", (lid,)
    ).fetchone())


class TestFullLifecycleSource:
    def test_disappearing_goes_straight_to_terminal(self, db):
        """feed 含 Reserved 时，消失只可能是 Niet beschikbaar。"""
        _add(db, "h1", "holland2stay", hours_ago=5)
        db.mark_stale_listings(cities=["Eindhoven"],
                               full_lifecycle_sources={"holland2stay"})
        assert _row(db, "h1")["status"] == "Occupied"

    def test_no_phantom_reserved_in_between(self, db):
        """刚过 reserved 窗口、还没到 occupied 窗口时，不该冒出一个推测的
        Reserved——平台从没说过这条房源被预留了。"""
        _add(db, "h1", "holland2stay", hours_ago=1.0)  # > 0.5h，< 2h
        db.mark_stale_listings(cities=["Eindhoven"],
                               reserved_hours=0.5, occupied_hours=2.0,
                               full_lifecycle_sources={"holland2stay"})
        assert _row(db, "h1")["status"] == "Available to book", \
            "消失 1 小时就被推成了 Reserved"

    def test_terminal_still_waits_out_the_window(self, db):
        """2 小时的余量是防抓取抖动的，不能因为判据变了就取消。"""
        _add(db, "h1", "holland2stay", hours_ago=0.1)
        db.mark_stale_listings(cities=["Eindhoven"], occupied_hours=2.0,
                               full_lifecycle_sources={"holland2stay"})
        assert _row(db, "h1")["status"] == "Available to book"

    def test_lottery_converges_the_same_way(self, db):
        _add(db, "h1", "holland2stay", status="Available in lottery", hours_ago=5)
        db.mark_stale_listings(cities=["Eindhoven"],
                               full_lifecycle_sources={"holland2stay"})
        assert _row(db, "h1")["status"] == "Occupied"


class TestOtherSourcesUnchanged:
    """Xior / OurDomain / OurCampus 的 feed 只列可订单元，判据不变。"""

    @pytest.mark.parametrize("source", ["xior", "ourdomain", "ourcampus"])
    def test_two_step_inference_preserved(self, db, source):
        _add(db, "s1", source, hours_ago=1.0)
        db.mark_stale_listings(cities=["Eindhoven"],
                               reserved_hours=0.5, occupied_hours=2.0,
                               full_lifecycle_sources={"holland2stay"})
        row = _row(db, "s1")
        assert row["status"] == "Reserved", f"{source} 的中间站被取消了"
        assert row["status_is_inferred"] == 1

    def test_reserved_still_ages_to_terminal(self, db):
        _add(db, "x1", "xior", status="Reserved", hours_ago=5)
        db.mark_stale_listings(cities=["Eindhoven"],
                               full_lifecycle_sources={"holland2stay"})
        assert _row(db, "x1")["status"] == "Occupied"

    def test_mixed_round_treats_each_source_by_its_own_rule(self, db):
        _add(db, "h1", "holland2stay", hours_ago=1.0)
        _add(db, "x1", "xior", hours_ago=1.0)
        db.mark_stale_listings(cities=["Eindhoven"],
                               reserved_hours=0.5, occupied_hours=2.0,
                               full_lifecycle_sources={"holland2stay"})
        assert _row(db, "h1")["status"] == "Available to book"
        assert _row(db, "x1")["status"] == "Reserved"


class TestDefaultIsTheOldBehaviour:
    """不传参数时维持旧判据——升级过程中配置还没改的那一刻不能变行为。"""

    def test_omitted_argument(self, db):
        _add(db, "h1", "holland2stay", hours_ago=1.0)
        db.mark_stale_listings(cities=["Eindhoven"],
                               reserved_hours=0.5, occupied_hours=2.0)
        assert _row(db, "h1")["status"] == "Reserved"

    def test_empty_set(self, db):
        _add(db, "h1", "holland2stay", hours_ago=1.0)
        db.mark_stale_listings(cities=["Eindhoven"],
                               reserved_hours=0.5, occupied_hours=2.0,
                               full_lifecycle_sources=set())
        assert _row(db, "h1")["status"] == "Reserved"


class TestConfigDerivesIt:
    """判据从实际配置推出，不写死平台名——AVAILABILITY_FILTERS 是可改的。"""

    def _cfg(self, ids, sources=("holland2stay",)):
        from config import AvailabilityFilter, Config
        from pathlib import Path

        return Config(
            check_interval=300, cities=[],
            availability_filters=[AvailabilityFilter(label=str(i), id=i) for i in ids],
            db_path=Path("x.db"), log_level="INFO", sources=list(sources),
        )

    def test_reserved_present(self):
        assert self._cfg([179, 336, 6203]).sources_with_full_lifecycle() == {
            "holland2stay"}

    def test_reserved_absent(self):
        assert self._cfg([179, 336]).sources_with_full_lifecycle() == frozenset()

    def test_h2s_not_monitored(self):
        assert self._cfg([179, 336, 6203],
                         sources=("xior",)).sources_with_full_lifecycle() == frozenset()

    def test_never_claims_other_sources(self):
        """Reserved 是 H2S 的属性 ID，不能因为它出现就替别的平台做主。"""
        got = self._cfg([179, 336, 6203],
                        sources=("holland2stay", "xior", "ourdomain")
                        ).sources_with_full_lifecycle()
        assert got == {"holland2stay"}
