"""被移出监控的城市，它的房源也要能收敛。

2026-08-04 发现：``mark_stale_listings`` 的范围限定（只收敛"本轮完整扫描成功
的城市"）有个副作用——一旦某个城市被移出监控，它就再也不会出现在完整扫描
名单里，于是**永远不会被收敛**，老化阈值根本没机会生效。

结果是每改一次监控城市就攒一批鬼影，最后一次见到已是几个月前，却还在列表和
地图上挂着"可订"。

这里盯三件事：
1. 掉出监控范围的确实会收敛（宽限期更长）；
2. **仍在监控、只是这轮没扫到**的不能被误杀——分片和节流会让正常城市这轮
   缺席，拿"本轮完整名单"当孤儿判据会误杀一大片；
3. 不知道监控范围时（配置读取失败）整条路径跳过，绝不能把整库判死。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mstorage import Storage


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


@pytest.fixture
def store(tmp_path):
    s = Storage(tmp_path / "t.db")
    yield s
    s.close()


def _add(st, lid, *, source, city, status="Available to book", days_ago=90):
    st.conn.execute(
        """INSERT INTO listings
           (id, name, status, price_raw, available_from, features, url, city,
            first_seen, last_seen, notified, last_status, source, status_is_inferred)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
        (lid, lid, status, "€1", "", "[]", f"u/{lid}", city,
         _iso(days_ago + 1), _iso(days_ago), 0, status, source),
    )
    st.conn.commit()


def _status(st, lid):
    row = st.conn.execute(
        "SELECT status, status_is_inferred FROM listings WHERE id=?", (lid,)
    ).fetchone()
    return (row[0], row[1])  # sqlite3.Row 不和 tuple 相等，转一下


MONITORED = [("holland2stay", "Eindhoven"), ("xior", "Amsterdam Naritaweg")]


class TestOrphanConvergence:
    def test_listing_in_dropped_city_converges(self, store):
        """这就是那批鬼影的处境。"""
        _add(store, "vaals", source="xior", city="Aachen Vaals Katzensprung", days_ago=90)

        n = store.mark_stale_listings(
            source_city_pairs=MONITORED, monitored_pairs=MONITORED, orphan_days=30,
        )

        assert n == 1
        assert _status(store, "vaals") == ("Occupied", 1)

    def test_reserved_also_converges_when_city_is_dropped(self, store):
        """一个完全不再观察的城市，Reserved 同样无从核实。

        注意这和"仍在监控"那条路径不同——那边 Reserved 是故意不收敛的
        （H2S 上 Reserved 本来就能挂很久）。
        """
        _add(store, "res", source="holland2stay", city="Nijmegen",
             status="Reserved", days_ago=90)

        store.mark_stale_listings(
            source_city_pairs=MONITORED, monitored_pairs=MONITORED, orphan_days=30,
        )

        assert _status(store, "res") == ("Occupied", 1)

    def test_within_grace_period_is_left_alone(self, store):
        """临时关一天再打开的城市不该被判死。"""
        _add(store, "recent", source="holland2stay", city="Rotterdam", days_ago=5)

        n = store.mark_stale_listings(
            source_city_pairs=MONITORED, monitored_pairs=MONITORED, orphan_days=30,
        )

        assert n == 0
        assert _status(store, "recent") == ("Available to book", 0)

    def test_already_occupied_is_not_touched(self, store):
        """幂等：跑多少次都不该重复计数。"""
        _add(store, "gone", source="xior", city="Utrecht Willem Dreeslaan",
             status="Occupied", days_ago=90)

        n = store.mark_stale_listings(
            source_city_pairs=MONITORED, monitored_pairs=MONITORED, orphan_days=30,
        )

        assert n == 0


class TestNoFriendlyFire:
    def test_monitored_city_absent_this_round_is_safe(self, store):
        """最危险的一种误杀。

        分片和节流会让一个正常监控的城市这轮不出现在"完整扫描"名单里。
        如果拿那份名单当孤儿判据，Xior 每轮只扫 3/4 栋楼，剩下那栋的房源
        每轮都会被判成孤儿。
        """
        _add(store, "naritaweg", source="xior", city="Amsterdam Naritaweg", days_ago=90)

        # 本轮只扫全了 Eindhoven，Naritaweg 缺席——但它仍在 monitored_pairs 里
        n = store.mark_stale_listings(
            source_city_pairs=[("holland2stay", "Eindhoven")],
            monitored_pairs=MONITORED,
            orphan_days=30,
        )

        assert n == 0, "仍在监控、只是这轮没扫到的城市被误杀了"
        assert _status(store, "naritaweg") == ("Available to book", 0)

    def test_same_city_name_on_another_source_is_not_confused(self, store):
        """孤儿判据必须是 (source, city) 组合，不能只看城市名。"""
        _add(store, "od-diemen", source="ourdomain", city="Amsterdam Diemen", days_ago=90)

        n = store.mark_stale_listings(
            source_city_pairs=[("ourcampus", "Amsterdam Diemen")],
            monitored_pairs=[("ourcampus", "Amsterdam Diemen")],
            orphan_days=30,
        )

        assert n == 1, "ourdomain 的 Amsterdam Diemen 已不在监控里，应当收敛"
        assert _status(store, "od-diemen") == ("Occupied", 1)


class TestFailOpen:
    @pytest.mark.parametrize("monitored", [None, []])
    def test_unknown_scope_skips_orphan_convergence(self, store, monitored):
        """不知道监控范围时不能把整库判成孤儿。

        ``_monitored_pairs`` 在读配置失败时返回空列表——那一刻整库看起来
        "全都不在监控范围内"。这里必须整条路径跳过。
        """
        _add(store, "anything", source="holland2stay", city="Rotterdam", days_ago=90)

        store.mark_stale_listings(
            source_city_pairs=MONITORED, monitored_pairs=monitored, orphan_days=30,
        )

        assert _status(store, "anything") == ("Available to book", 0)


class TestAgingPathStillWorks:
    """孤儿那条分支不能把老化那条带偏——两者范围互斥，各管各的。

    老化的阈值本身已经从「7 天 / 2 天」改成统一的「30 分钟 → Reserved，
    2 小时 → Occupied」，细节见 tests/test_stale_convergence.py。
    """

    def test_monitored_city_still_ages(self, store):
        _add(store, "eind-book", source="holland2stay", city="Eindhoven", days_ago=10)
        _add(store, "eind-lot", source="holland2stay", city="Eindhoven",
             status="Available in lottery", days_ago=3)
        _add(store, "eind-fresh", source="holland2stay", city="Eindhoven", days_ago=0)

        store.mark_stale_listings(
            source_city_pairs=MONITORED, monitored_pairs=MONITORED,
        )

        assert _status(store, "eind-book") == ("Reserved", 1)
        assert _status(store, "eind-lot") == ("Reserved", 1)
        assert _status(store, "eind-fresh") == ("Available to book", 0)

    def test_a_long_vanished_platform_reserved_also_converges(self, store):
        """前提变了，记一下为什么。

        原来这里断言的是「H2S 的 Reserved 在监控范围内永不收敛」，于是一批
        消失了好几个月的记录永远卡着。

        现在它也会收敛：H2S 的 Reserved 是真实状态（有人下单未付款），但那个
        状态**有硬性时限**——官方付款限时 2 小时，超时未付就作废并重新上架。
        所以一条消失超过 2 小时的 Reserved，必然已经落定。
        """
        _add(store, "eind-res", source="holland2stay", city="Eindhoven",
             status="Reserved", days_ago=90)

        store.mark_stale_listings(
            source_city_pairs=MONITORED, monitored_pairs=MONITORED,
        )

        assert _status(store, "eind-res") == ("Occupied", 1)

    def test_no_scope_at_all_behaves_as_before(self, store):
        """完全不传范围时是老语义：全库按老化阈值收敛。"""
        _add(store, "x", source="holland2stay", city="Wherever", days_ago=90)

        n = store.mark_stale_listings()

        assert n == 1
