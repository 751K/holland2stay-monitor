"""tools/converge_plaza_extra_aanbod.py —— 一次性收敛脚本

这个脚本存在的唯一理由是**不要发出 31 条假的「下架」通知**。所以测试盯的就是那件事：
跑完之后 ``diff()`` 必须无事可做，且 ``status_changes`` 里没有残留的待通知行。
"""
import sqlite3

import pytest

from models import Listing
import tools.converge_plaza_extra_aanbod as conv


def _listing(lid: str, status: str) -> Listing:
    return Listing(id=lid, name=f"Adres {lid}", status=status,
                   price_raw="€ 900,00", available_from=None, features=[],
                   url="https://x", city="Utrecht", source="plaza")


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """库里三条 plaza：两条将被降级，一条保持可订。

    脚本按 ``DATA_DIR / "listings.db"`` 找库，所以这里自建一个同名的，
    而不是复用 ``temp_db``（那个叫 test.db）。
    """
    from storage import Storage

    db = Storage(tmp_path / "listings.db", timezone_str="UTC")
    db.diff([_listing("pz_1", "Available to book"),
             _listing("pz_2", "Available to book"),
             _listing("pz_3", "Available to book")])
    monkeypatch.setattr(conv, "DATA_DIR", tmp_path)
    monkeypatch.setattr(
        conv, "_scraped_statuses",
        lambda: {"pz_1": "Not available", "pz_2": "Not available",
                 "pz_3": "Available to book"})
    db.path = tmp_path / "listings.db"
    yield db
    db.close()


def _statuses(db_path):
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        return {r["id"]: r["status"]
                for r in con.execute("SELECT id, status FROM listings")}
    finally:
        con.close()


class TestConverge:
    def test_dry_run_changes_nothing(self, seeded, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", ["conv"])
        assert conv.main() == 0
        assert set(_statuses(seeded.path).values()) == {"Available to book"}
        assert "未写入" in capsys.readouterr().out

    def test_apply_demotes_only_the_flagged_ones(self, seeded, monkeypatch):
        monkeypatch.setattr("sys.argv", ["conv", "--apply"])
        assert conv.main() == 0
        assert _statuses(seeded.path) == {
            "pz_1": "Not available",
            "pz_2": "Not available",
            "pz_3": "Available to book",
        }

    def test_after_converging_diff_produces_no_event(self, seeded, monkeypatch):
        """这条是整个脚本的目的：收敛之后新 scraper 抓回来，不产出任何状态变化。"""
        monkeypatch.setattr("sys.argv", ["conv", "--apply"])
        conv.main()

        new, changes = seeded.diff([_listing("pz_1", "Not available"),
                                    _listing("pz_2", "Not available"),
                                    _listing("pz_3", "Available to book")])
        assert new == [] and changes == []

    def test_without_converging_diff_would_have_notified(self, seeded):
        """反证：不跑脚本，同一批数据会产出待通知的状态变化。

        没有这条，上面那条「无事件」可能只是因为 diff() 本来就不报这种变化——
        那样整个脚本都是白写的。
        """
        _, changes = seeded.diff([_listing("pz_1", "Not available"),
                                  _listing("pz_2", "Not available"),
                                  _listing("pz_3", "Available to book")])
        assert sorted(c[0].id for c in changes) == ["pz_1", "pz_2"]
        assert [c[0].id for c in seeded.pending_status_changes()] != []

    def test_pending_changes_are_suppressed(self, seeded, monkeypatch):
        """monitor 抢先跑过一轮的情况：事件已在库里，脚本要把它们收掉。"""
        seeded.diff([_listing("pz_1", "Not available"),
                     _listing("pz_2", "Not available"),
                     _listing("pz_3", "Available to book")])
        assert seeded.pending_status_changes(), "前置条件不成立，这条会空过"

        monkeypatch.setattr("sys.argv", ["conv", "--apply"])
        conv.main()
        assert seeded.pending_status_changes() == []

    def test_an_incomplete_response_refuses_to_touch_the_db(self, temp_db, monkeypatch):
        """探针不过 / 解析 0 条时必须抛，不能把「没抓到」当成「全部下架」。"""
        from scrapers.plaza import PlazaScraper

        monkeypatch.setattr(PlazaScraper, "_fetch", lambda self: {})
        monkeypatch.setattr(PlazaScraper, "_parse_all",
                            lambda self, payload: ([], False))
        with pytest.raises(RuntimeError, match="完整性探针"):
            conv._scraped_statuses()

        monkeypatch.setattr(PlazaScraper, "_parse_all",
                            lambda self, payload: ([], True))
        with pytest.raises(RuntimeError, match="0 条"):
            conv._scraped_statuses()
