"""monitor Phase 3 stale listing 收敛测试。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from models import Listing
from monitor import _mark_stale_listings_for_complete_cities, _stale_sweep_decision


def _listing(
    listing_id: str,
    *,
    city: str,
    status: str = "Available to book",
    source: str = "holland2stay",
) -> Listing:
    return Listing(
        id=listing_id,
        name=f"Listing {listing_id}",
        status=status,
        price_raw="€1000",
        available_from="2030-01-01",
        features=[],
        url=f"https://example.test/{listing_id}",
        city=city,
        source=source,
    )


def _set_last_seen(temp_db, listing_id: str, days_ago: int) -> None:
    last_seen = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    with temp_db.conn:
        temp_db.conn.execute(
            "UPDATE listings SET last_seen=? WHERE id=?",
            (last_seen, listing_id),
        )


class TestMonitorStaleSweep:
    def test_marks_only_complete_cities(self, temp_db):
        temp_db.diff([
            _listing("e", city="Eindhoven"),
            _listing("a", city="Amsterdam"),
        ])
        _set_last_seen(temp_db, "e", 8)
        _set_last_seen(temp_db, "a", 8)

        updated = _mark_stale_listings_for_complete_cities(
            temp_db,
            {"Eindhoven": True, "Amsterdam": False},
            days=7,
        )

        assert updated == 1
        assert temp_db.get_listing("e")["status"] == "Occupied"
        assert temp_db.get_listing("a")["status"] == "Available to book"

    def test_no_complete_city_is_noop(self, temp_db):
        temp_db.diff([_listing("e", city="Eindhoven")])
        _set_last_seen(temp_db, "e", 8)

        updated = _mark_stale_listings_for_complete_cities(
            temp_db,
            {"Eindhoven": False},
            days=7,
        )

        assert updated == 0
        assert temp_db.get_listing("e")["status"] == "Available to book"

    def test_empty_completeness_is_noop(self, temp_db):
        temp_db.diff([_listing("e", city="Eindhoven")])
        _set_last_seen(temp_db, "e", 8)

        updated = _mark_stale_listings_for_complete_cities(temp_db, {}, days=7)

        assert updated == 0
        assert temp_db.get_listing("e")["status"] == "Available to book"

    def test_lottery_window_passed_to_storage(self, temp_db):
        temp_db.diff([
            _listing("book", city="Eindhoven", status="Available to book"),
            _listing("lottery", city="Eindhoven", status="Available in lottery"),
        ])
        _set_last_seen(temp_db, "book", 3)
        _set_last_seen(temp_db, "lottery", 3)

        updated = _mark_stale_listings_for_complete_cities(
            temp_db,
            {"Eindhoven": True},
            days=7,
            lottery_days=2,
        )

        assert updated == 1
        assert temp_db.get_listing("book")["status"] == "Available to book"
        assert temp_db.get_listing("lottery")["status"] == "Occupied"

    def test_source_prefixed_completeness_limits_stale_to_source_city(self, temp_db):
        temp_db.diff([
            _listing("h2s", city="Amsterdam Diemen", source="holland2stay"),
            _listing("od", city="Amsterdam Diemen", source="ourdomain"),
        ])
        _set_last_seen(temp_db, "h2s", 8)
        _set_last_seen(temp_db, "od", 8)

        updated = _mark_stale_listings_for_complete_cities(
            temp_db,
            {"ourdomain:Amsterdam Diemen": True},
            days=7,
        )

        assert updated == 1
        assert temp_db.get_listing("h2s")["status"] == "Available to book"
        assert temp_db.get_listing("od")["status"] == "Occupied"


# ── 24h 计时器不该被空轮消耗 ───────────────────────────────────────

_DAY = 24 * 60 * 60


class TestStaleSweepDecision:
    """到点了但本轮没有完整城市时，必须 defer 而不是把计时器用掉。

    原代码在 ``finally`` 里无条件 ``last_stale_sweep_time = time.monotonic()``，
    于是 run_once 的兜底路径（未分类错误 / 管线错误返回 ``{}``）或 H2S 熔断期
    只要撞上 24 小时那一轮，收敛就被白白跳过，鬼影 listing 再多挂一天。
    """

    def test_not_due_yet(self):
        assert _stale_sweep_decision({"E": True}, 0.0, _DAY, now=_DAY - 1) == "wait"

    def test_due_with_complete_city_runs(self):
        assert _stale_sweep_decision({"E": True}, 0.0, _DAY, now=_DAY) == "run"

    def test_due_but_empty_completeness_defers(self):
        """run_once 兜底路径返回 {} —— 抓取阶段未分类错误 / 管线错误。"""
        assert _stale_sweep_decision({}, 0.0, _DAY, now=_DAY) == "defer"

    def test_due_but_all_cities_incomplete_defers(self):
        assert _stale_sweep_decision(
            {"E": False, "A": False}, 0.0, _DAY, now=_DAY,
        ) == "defer"

    def test_partial_completeness_still_runs(self):
        """有一个完整城市就值得跑——收敛本来就是按城市 scope 的。"""
        assert _stale_sweep_decision(
            {"E": True, "A": False}, 0.0, _DAY, now=_DAY,
        ) == "run"

    def test_defer_does_not_consume_timer(self):
        """defer 之后计时器没变，下一轮只要有完整城市就能立刻补跑。"""
        last = 0.0
        assert _stale_sweep_decision({}, last, _DAY, now=_DAY + 60) == "defer"
        # 调用方在 defer 分支不更新 last_sweep_at，所以下一轮仍然是 due
        assert _stale_sweep_decision({"E": True}, last, _DAY, now=_DAY + 120) == "run"
