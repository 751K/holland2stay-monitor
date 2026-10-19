"""老化收敛：消失 30 分钟转推测 Reserved，消失 2 小时判 Occupied。

四个平台、所有状态**统一一套**。曾经按 (source, 状态类) 分开配过——理由是
「有的平台 feed 会保留下架房源」——最后收掉了：实测下来四个平台的终态都是
**从 feed 里消失**——只有 Xior 的 feed 里真有 Occupied，其余三个平台的终态基本
全靠推。既然消失是共同的下架信号，就不该有四套判据。

为什么中间要有 Reserved 这一站
------------------------------
「没见到了」够强到不该再当可订，但不足以断言「已出租」。直接跳终态是把推断当
事实：判错时房源从面板上彻底消失，等 feed 恢复再出现会产生 ``Occupied → 可订``，
用户收到一批假的「重新上架」——**收得太早的代价是多通知，不是少通知**。

落在 Reserved 上代价小得多：``Listing.is_available`` 不含它（「不再显示为可订」
这件正事第一段就办到了），而 ``Reserved → 可订`` 在 H2S 上本来就是最常见的正常
迁移之一，语义是「别人的预留没成」，不是一次突兀的复活。

原阈值错在哪
------------
- OurDomain 的房源寿命是小时级（一批出现，一条条被订走），而原阈值 7 天，
  中间那一周它在列表、地图和 API 上都还挂着「可订」；
- H2S 抽签几乎不产生「从抽签迁出」的状态变更，全都是直接消失，于是抽签房源
  消失一天多之后仍挂着「可抽签」；
- 平台报的 Reserved 原来不参与收敛，一批消失了好几个月的记录因此永远卡着。

2 小时是从哪来的
----------------
**H2S 官方的付款限时就是 2 小时。** 这个数同时管住两种 Reserved：我们推出来的
（消失了但还没到终态），和平台自己报的（有人下单未付款）。一条已经消失超过
2 小时的 Reserved，付款窗口必然已经关闭——要么付成了，要么作废了；作废的话它会
以「可订」重新出现在 feed 里，而我们没看到它。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mstorage import Storage
from models import Listing


def _iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


@pytest.fixture
def store(tmp_path):
    s = Storage(tmp_path / "t.db")
    yield s
    s.close()


def _add(st, lid, *, source="ourdomain", city="Amsterdam Diemen",
         status="Available to book", hours_ago=5.0, inferred=0):
    st.conn.execute(
        """INSERT INTO listings
           (id, name, status, price_raw, available_from, features, url, city,
            first_seen, last_seen, notified, last_status, source, status_is_inferred)
           VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?,?)""",
        (lid, lid, status, "€1", "", "[]", f"u/{lid}", city,
         _iso(hours_ago + 1), _iso(hours_ago), status, source, inferred),
    )
    st.conn.commit()


def _status(st, lid):
    row = st.conn.execute(
        "SELECT status, status_is_inferred FROM listings WHERE id=?", (lid,)
    ).fetchone()
    return (row[0], row[1])


OD = ("ourdomain", "Amsterdam Diemen")
H2S = ("holland2stay", "Eindhoven")
XIOR = ("xior", "Amsterdam Naritaweg")
OC = ("ourcampus", "OurCampus Amsterdam Diemen")
SCOPE = [OD, H2S, XIOR, OC]


def _sweep(st, **kw):
    kw.setdefault("source_city_pairs", SCOPE)
    return st.mark_stale_listings(**kw)


class TestFirstStage:
    @pytest.mark.parametrize("source,city", SCOPE)
    def test_every_platform_goes_the_same_way(self, store, source, city):
        """统一的意思就是不看 source。"""
        _add(store, "x", source=source, city=city, hours_ago=1)

        assert _sweep(store) == 1
        assert _status(store, "x") == ("Reserved", 1)

    @pytest.mark.parametrize(
        "status", ["Available to book", "Available in lottery", "Unknown"],
    )
    def test_every_available_status_goes_the_same_way(self, store, status):
        _add(store, "x", status=status, hours_ago=1)

        assert _sweep(store) == 1
        assert _status(store, "x") == ("Reserved", 1)

    def test_it_is_no_longer_available(self, store):
        """第一段要办成的正事：别再当可订。"""
        from models import Listing

        _add(store, "x", hours_ago=1)
        _sweep(store)
        status = store.conn.execute(
            "SELECT status FROM listings WHERE id='x'"
        ).fetchone()[0]
        l = Listing(id="x", name="x", status=status, price_raw=None,
                    available_from=None, features=[], url="", city="",
                    source="ourdomain")
        assert l.is_available is False

    def test_within_the_window_is_untouched(self, store):
        _add(store, "x", hours_ago=0.2)

        assert _sweep(store) == 0
        assert _status(store, "x") == ("Available to book", 0)


class TestSecondStage:
    def test_reserved_falls_to_occupied(self, store):
        _add(store, "x", status="Reserved", hours_ago=3, inferred=1)

        assert _sweep(store) == 1
        assert _status(store, "x") == ("Occupied", 1)

    def test_not_before_the_window(self, store):
        _add(store, "x", status="Reserved", hours_ago=1, inferred=1)

        assert _sweep(store) == 0
        assert _status(store, "x") == ("Reserved", 1)

    def test_platform_reported_reserved_uses_the_same_window(self, store):
        """不分是谁说的——官方付款限时 2 小时，消失超过它就必然已经落定。

        H2S 的 Reserved 是真实状态（有人下单未付款），但那个状态**有硬性时限**：
        2 小时内没付完就作废，作废的话它会以「可订」重新出现在 feed 里。所以
        「消失超过 2 小时」对两种 Reserved 都意味着同一件事。
        """
        _add(store, "plat", source="holland2stay", city="Eindhoven",
             status="Reserved", hours_ago=3, inferred=0)
        _add(store, "ours", source="holland2stay", city="Eindhoven",
             status="Reserved", hours_ago=3, inferred=1)

        assert _sweep(store) == 2
        assert _status(store, "plat") == ("Occupied", 1)
        assert _status(store, "ours") == ("Occupied", 1)

    def test_a_fresh_platform_reserved_is_untouched(self, store):
        """还在付款窗口里、而且还在 feed 里的，不动。"""
        _add(store, "plat", source="holland2stay", city="Eindhoven",
             status="Reserved", hours_ago=0.05, inferred=0)

        assert _sweep(store) == 0
        assert _status(store, "plat") == ("Reserved", 0)

    def test_a_reserved_still_in_the_feed_is_untouched(self, store):
        """这才是「平台报的 Reserved 不该被动」真正的保护机制。

        H2S 的 Reserved 常态就长，而且绝大多数最后会回到「可订」。只要它还在
        feed 里被列出，last_seen 每轮刷新，这条路径就永远够不着它。
        """
        _add(store, "res", source="holland2stay", city="Eindhoven",
             status="Reserved", hours_ago=0.05, inferred=0)

        assert _sweep(store) == 0
        assert _status(store, "res") == ("Reserved", 0)

    def test_one_step_per_sweep(self, store):
        """一条消失很久的房源，一次收敛只走一步。

        反过来（第一段先跑）会让它在同一次调用里被连改两次，返回的行数把它
        算两遍，「本轮收敛了几条」就不再等于「几条房源变了状态」。
        """
        _add(store, "x", hours_ago=24 * 8)

        assert _sweep(store) == 1
        assert _status(store, "x") == ("Reserved", 1)
        assert _sweep(store) == 1
        assert _status(store, "x") == ("Occupied", 1)
        assert _sweep(store) == 0

    def test_idempotent_at_the_terminal_state(self, store):
        _add(store, "x", status="Occupied", hours_ago=24 * 90, inferred=1)

        assert _sweep(store) == 0


class TestScope:
    def test_incomplete_city_is_untouched(self, store):
        """没扫全就不能说「没见到 = 没了」。"""
        _add(store, "x", hours_ago=3)

        assert store.mark_stale_listings(source_city_pairs=[H2S]) == 0
        assert _status(store, "x") == ("Available to book", 0)

    def test_same_city_on_another_source_is_not_confused(self, store):
        _add(store, "od", source="ourdomain", city="Amsterdam Diemen", hours_ago=3)

        store.mark_stale_listings(
            source_city_pairs=[("ourcampus", "Amsterdam Diemen")],
        )

        assert _status(store, "od") == ("Available to book", 0)

    def test_city_only_scope_works(self, store):
        _add(store, "x", hours_ago=3)

        assert store.mark_stale_listings(cities=["Amsterdam Diemen"]) == 1

    def test_empty_scope_updates_nothing(self, store):
        _add(store, "x", hours_ago=24 * 90)

        assert store.mark_stale_listings(source_city_pairs=[]) == 0
        assert _status(store, "x") == ("Available to book", 0)

    def test_no_scope_at_all_sweeps_everything(self, store):
        """完全不传范围 = 老语义，全库按阈值收敛。"""
        _add(store, "x", source="whatever", city="Wherever", hours_ago=3)

        assert store.mark_stale_listings() == 1


class TestGuardrails:
    def test_zero_hours_does_not_wipe_everything(self, store):
        """配成 0 会把整个监控范围当场判死，而且日志里看不出来——
        只会表现成「房源突然全没了」。15 分钟的硬下限兜住。"""
        _add(store, "x", hours_ago=0.05)

        assert _sweep(store, reserved_hours=0.0, occupied_hours=0.0) == 0
        assert _status(store, "x") == ("Available to book", 0)

    def test_orphan_path_is_opt_in(self, store):
        """不传 monitored_pairs = 不知道监控范围 → 跳过孤儿收敛。

        fail-open：宁可留着鬼影，也不能因为一次配置读取失败把整库判死。
        """
        _add(store, "orphan", source="xior", city="Aachen Vaals", hours_ago=24 * 90)

        _sweep(store)

        assert _status(store, "orphan") == ("Available to book", 0)

    def test_orphan_path_converges_when_scope_is_known(self, store):
        _add(store, "orphan", source="xior", city="Aachen Vaals", hours_ago=24 * 90)

        _sweep(store, monitored_pairs=SCOPE, orphan_days=30)

        assert _status(store, "orphan") == ("Occupied", 1)


class TestRecovery:
    def test_reappearing_listing_comes_back_clean(self, store):
        """判错时的代价：feed 再看到它，状态和推测标记都复位。

        而且回来时是 ``Reserved → 可订``——正常迁移，不是一次突兀的复活。
        """
        from models import Listing

        _add(store, "x", hours_ago=1)
        _sweep(store)
        assert _status(store, "x") == ("Reserved", 1)

        fresh = Listing(
            id="x", name="x", status="Available to book", price_raw="€1",
            available_from=None, features=[], url="u/x",
            city="Amsterdam Diemen", source="ourdomain",
        )
        _new, changes = store.diff([fresh])

        assert _status(store, "x") == ("Available to book", 0)
        assert changes and changes[0][1] == "Reserved"


class TestMonitorWiring:
    def test_defaults(self):
        from monitor import _stale_hours

        assert _stale_hours() == (0.5, 2.0), "2 小时对齐 H2S 官方付款限时"

    def test_env_override(self, monkeypatch):
        from monitor import _stale_hours

        monkeypatch.setenv("STALE_RESERVED_HOURS", "1")
        monkeypatch.setenv("STALE_OCCUPIED_HOURS", "6")
        assert _stale_hours() == (1.0, 6.0)

    def test_garbage_env_falls_back(self, monkeypatch):
        from monitor import _stale_hours

        monkeypatch.setenv("STALE_RESERVED_HOURS", "半小时")
        assert _stale_hours()[0] == 0.5

    def test_occupied_never_precedes_reserved(self, monkeypatch):
        """终态窗口比中间站还短的话，第二段会抢在第一段之前把房源直接判死，
        Reserved 那一站形同虚设——而它正是「判错时代价小」的全部来源。"""
        from monitor import _stale_hours

        monkeypatch.setenv("STALE_RESERVED_HOURS", "6")
        monkeypatch.setenv("STALE_OCCUPIED_HOURS", "1")
        reserved, occupied = _stale_hours()
        assert occupied >= reserved

    def test_the_per_round_pass_does_not_do_orphan_convergence(self, store):
        """孤儿收敛要扫全库、宽限期 30 天，没有理由每轮跑，留在 24 小时那趟。

        盯的是**调用参数**而不是结果：把 ``monitored_pairs`` 顺手传进这一趟，
        功能上看不出错（孤儿本来也该被收敛），只是不该由每轮跑的路径做。
        """
        from monitor import _sweep_aging

        _add(store, "orphan", source="xior", city="Aachen Vaals", hours_ago=24 * 90)

        _sweep_aging(store, {"ourdomain:Amsterdam Diemen": True})

        assert _status(store, "orphan") == ("Available to book", 0)

    def test_the_per_round_pass_converges(self, store):
        from monitor import _sweep_aging

        _add(store, "x", hours_ago=1)

        assert _sweep_aging(store, {"ourdomain:Amsterdam Diemen": True}) == 1
        assert _status(store, "x") == ("Reserved", 1)

    def test_repeated_passes_are_no_ops(self, store):
        """每轮跑没有累积开销：到终态的行之后一律被 WHERE 排除。"""
        from monitor import _sweep_aging

        _add(store, "x", hours_ago=3)
        comp = {"ourdomain:Amsterdam Diemen": True}

        assert _sweep_aging(store, comp) == 1     # → Reserved
        assert _sweep_aging(store, comp) == 1     # → Occupied
        assert _sweep_aging(store, comp) == 0
        assert _sweep_aging(store, comp) == 0


# ── 被移出监控的城市也要收敛（原 test_stale_orphan_cities.py） ──────
#
# 2026-08-04 发现：``mark_stale_listings`` 的范围限定（只收敛「本轮完整扫描成功
# 的城市」）有个副作用——一旦某个城市被移出监控，它就再也不会出现在完整扫描
# 名单里，于是**永远不会被收敛**，老化阈值根本没机会生效。
#
# 结果是每改一次监控城市就攒一批鬼影，最后一次见到已是几个月前，却还在列表和
# 地图上挂着「可订」。
#
# 这里盯三件事：
# 1. 掉出监控范围的确实会收敛（宽限期更长）；
# 2. **仍在监控、只是这轮没扫到**的不能被误杀——分片和节流会让正常城市这轮
#    缺席，拿「本轮完整名单」当孤儿判据会误杀一大片；
# 3. 不知道监控范围时（配置读取失败）整条路径跳过，绝不能把整库判死。

def _iso_days(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _add_days(st, lid, *, source, city, status="Available to book", days_ago=90):
    st.conn.execute(
        """INSERT INTO listings
           (id, name, status, price_raw, available_from, features, url, city,
            first_seen, last_seen, notified, last_status, source, status_is_inferred)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
        (lid, lid, status, "€1", "", "[]", f"u/{lid}", city,
         _iso_days(days_ago + 1), _iso_days(days_ago), 0, status, source),
    )
    st.conn.commit()


MONITORED = [("holland2stay", "Eindhoven"), ("xior", "Amsterdam Naritaweg")]


class TestOrphanConvergence:
    def test_listing_in_dropped_city_converges(self, store):
        """这就是那批鬼影的处境。"""
        _add_days(store, "vaals", source="xior", city="Aachen Vaals Katzensprung", days_ago=90)

        n = store.mark_stale_listings(
            source_city_pairs=MONITORED, monitored_pairs=MONITORED, orphan_days=30,
        )

        assert n == 1
        assert _status(store, "vaals") == ("Occupied", 1)

    def test_reserved_also_converges_when_city_is_dropped(self, store):
        """一个完全不再观察的城市，Reserved 同样无从核实。

        注意这和「仍在监控」那条路径不同——那边 Reserved 是故意不收敛的
        （H2S 上 Reserved 本来就能挂很久）。
        """
        _add_days(store, "res", source="holland2stay", city="Nijmegen",
                  status="Reserved", days_ago=90)

        store.mark_stale_listings(
            source_city_pairs=MONITORED, monitored_pairs=MONITORED, orphan_days=30,
        )

        assert _status(store, "res") == ("Occupied", 1)

    def test_within_grace_period_is_left_alone(self, store):
        """临时关一天再打开的城市不该被判死。"""
        _add_days(store, "recent", source="holland2stay", city="Rotterdam", days_ago=5)

        n = store.mark_stale_listings(
            source_city_pairs=MONITORED, monitored_pairs=MONITORED, orphan_days=30,
        )

        assert n == 0
        assert _status(store, "recent") == ("Available to book", 0)

    def test_already_occupied_is_not_touched(self, store):
        """幂等：跑多少次都不该重复计数。"""
        _add_days(store, "gone", source="xior", city="Utrecht Willem Dreeslaan",
                  status="Occupied", days_ago=90)

        n = store.mark_stale_listings(
            source_city_pairs=MONITORED, monitored_pairs=MONITORED, orphan_days=30,
        )

        assert n == 0


class TestNoFriendlyFire:
    def test_monitored_city_absent_this_round_is_safe(self, store):
        """最危险的一种误杀。

        分片和节流会让一个正常监控的城市这轮不出现在「完整扫描」名单里。
        如果拿那份名单当孤儿判据，Xior 每轮只扫 3/4 栋楼，剩下那栋的房源
        每轮都会被判成孤儿。
        """
        _add_days(store, "naritaweg", source="xior", city="Amsterdam Naritaweg", days_ago=90)

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
        _add_days(store, "od-diemen", source="ourdomain", city="Amsterdam Diemen", days_ago=90)

        n = store.mark_stale_listings(
            source_city_pairs=[("ourcampus", "Amsterdam Diemen")],
            monitored_pairs=[("ourcampus", "Amsterdam Diemen")],
            orphan_days=30,
        )

        assert n == 1, "ourdomain 的 Amsterdam Diemen 已不在监控里，应当收敛"
        assert _status(store, "od-diemen") == ("Occupied", 1)


class TestOrphanFailOpen:
    @pytest.mark.parametrize("monitored", [None, []])
    def test_unknown_scope_skips_orphan_convergence(self, store, monitored):
        """不知道监控范围时不能把整库判成孤儿。

        ``_monitored_pairs`` 在读配置失败时返回空列表——那一刻整库看起来
        「全都不在监控范围内」。这里必须整条路径跳过。
        """
        _add_days(store, "anything", source="holland2stay", city="Rotterdam", days_ago=90)

        store.mark_stale_listings(
            source_city_pairs=MONITORED, monitored_pairs=monitored, orphan_days=30,
        )

        assert _status(store, "anything") == ("Available to book", 0)


class TestAgingPathStillWorks:
    """孤儿那条分支不能把老化那条带偏——两者范围互斥，各管各的。"""

    def test_monitored_city_still_ages(self, store):
        _add_days(store, "eind-book", source="holland2stay", city="Eindhoven", days_ago=10)
        _add_days(store, "eind-lot", source="holland2stay", city="Eindhoven",
                  status="Available in lottery", days_ago=3)
        _add_days(store, "eind-fresh", source="holland2stay", city="Eindhoven", days_ago=0)

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
        _add_days(store, "eind-res", source="holland2stay", city="Eindhoven",
                  status="Reserved", days_ago=90)

        store.mark_stale_listings(
            source_city_pairs=MONITORED, monitored_pairs=MONITORED,
        )

        assert _status(store, "eind-res") == ("Occupied", 1)

    def test_no_scope_at_all_behaves_as_before(self, store):
        """完全不传范围时是老语义：全库按老化阈值收敛。"""
        _add_days(store, "x", source="holland2stay", city="Wherever", days_ago=90)

        n = store.mark_stale_listings()

        assert n == 1


# ── feed 覆盖 Reserved 后的消失语义（原 test_full_lifecycle_states.py） ──
#
# H2S 的 ``available_to_book`` 有六个取值，我们原先只抓两个（可订 179、抽签
# 336）。那种配置下「消失」是**有歧义**的——可能被人下单了（Reserved 6203），
# 也可能彻底没了（Niet beschikbaar 180），所以先推 Reserved 留出 2 小时付款
# 窗口，再判终态。
#
# 把 Reserved 也抓进来之后，消失就没有歧义了：它已经掉出我们跟踪的全部状态。
# 此时再推一次 Reserved，是凭空造一个平台从没说过的状态，还会把
# ``status_is_inferred=1`` 打在一条本可以如实上报的房源上。
#
# 其余三个平台的 feed 只列可订单元，没有等价的「已预留」可抓，判据不变。

def _iso_lifecycle(**delta):
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


@pytest.fixture
def db(tmp_path):
    st = Storage(tmp_path / "listings.db")
    yield st
    st.close()


def _add_lifecycle(st, lid, source, status="Available to book", hours_ago=5.0):
    st.diff([Listing(id=lid, name=lid, status=status, price_raw="1000",
                     available_from="", url="", features=[],
                     city="Eindhoven", source=source)])
    st.conn.execute("UPDATE listings SET last_seen=? WHERE id=?",
                    (_iso_lifecycle(hours=hours_ago), lid))
    st.conn.commit()


def _row(st, lid):
    return dict(st.conn.execute(
        "SELECT status, status_is_inferred FROM listings WHERE id=?", (lid,)
    ).fetchone())


class TestFullLifecycleSource:
    def test_disappearing_goes_straight_to_terminal(self, db):
        """feed 含 Reserved 时，消失只可能是 Niet beschikbaar。"""
        _add_lifecycle(db, "h1", "holland2stay", hours_ago=5)
        db.mark_stale_listings(cities=["Eindhoven"],
                               full_lifecycle_sources={"holland2stay"})
        assert _row(db, "h1")["status"] == "Occupied"

    def test_no_phantom_reserved_in_between(self, db):
        """刚过 reserved 窗口、还没到 occupied 窗口时，不该冒出一个推测的
        Reserved——平台从没说过这条房源被预留了。"""
        _add_lifecycle(db, "h1", "holland2stay", hours_ago=1.0)  # > 0.5h，< 2h
        db.mark_stale_listings(cities=["Eindhoven"],
                               reserved_hours=0.5, occupied_hours=2.0,
                               full_lifecycle_sources={"holland2stay"})
        assert _row(db, "h1")["status"] == "Available to book", \
            "消失 1 小时就被推成了 Reserved"

    def test_terminal_still_waits_out_the_window(self, db):
        """2 小时的余量是防抓取抖动的，不能因为判据变了就取消。"""
        _add_lifecycle(db, "h1", "holland2stay", hours_ago=0.1)
        db.mark_stale_listings(cities=["Eindhoven"], occupied_hours=2.0,
                               full_lifecycle_sources={"holland2stay"})
        assert _row(db, "h1")["status"] == "Available to book"

    def test_lottery_converges_the_same_way(self, db):
        _add_lifecycle(db, "h1", "holland2stay", status="Available in lottery", hours_ago=5)
        db.mark_stale_listings(cities=["Eindhoven"],
                               full_lifecycle_sources={"holland2stay"})
        assert _row(db, "h1")["status"] == "Occupied"


class TestOtherSourcesUnchanged:
    """Xior / OurDomain / OurCampus 的 feed 只列可订单元，判据不变。"""

    @pytest.mark.parametrize("source", ["xior", "ourdomain", "ourcampus"])
    def test_two_step_inference_preserved(self, db, source):
        _add_lifecycle(db, "s1", source, hours_ago=1.0)
        db.mark_stale_listings(cities=["Eindhoven"],
                               reserved_hours=0.5, occupied_hours=2.0,
                               full_lifecycle_sources={"holland2stay"})
        row = _row(db, "s1")
        assert row["status"] == "Reserved", f"{source} 的中间站被取消了"
        assert row["status_is_inferred"] == 1

    def test_reserved_still_ages_to_terminal(self, db):
        _add_lifecycle(db, "x1", "xior", status="Reserved", hours_ago=5)
        db.mark_stale_listings(cities=["Eindhoven"],
                               full_lifecycle_sources={"holland2stay"})
        assert _row(db, "x1")["status"] == "Occupied"

    def test_mixed_round_treats_each_source_by_its_own_rule(self, db):
        _add_lifecycle(db, "h1", "holland2stay", hours_ago=1.0)
        _add_lifecycle(db, "x1", "xior", hours_ago=1.0)
        db.mark_stale_listings(cities=["Eindhoven"],
                               reserved_hours=0.5, occupied_hours=2.0,
                               full_lifecycle_sources={"holland2stay"})
        assert _row(db, "h1")["status"] == "Available to book"
        assert _row(db, "x1")["status"] == "Reserved"


class TestDefaultIsTheOldBehaviour:
    """不传参数时维持旧判据——升级过程中配置还没改的那一刻不能变行为。"""

    def test_omitted_argument(self, db):
        _add_lifecycle(db, "h1", "holland2stay", hours_ago=1.0)
        db.mark_stale_listings(cities=["Eindhoven"],
                               reserved_hours=0.5, occupied_hours=2.0)
        assert _row(db, "h1")["status"] == "Reserved"

    def test_empty_set(self, db):
        _add_lifecycle(db, "h1", "holland2stay", hours_ago=1.0)
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
