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
