"""city_normalized 的落库、回填与查询。

归一表写在 config 里，而房源已经在库里躺着。三件事必须成立：
1. 新写入的行带上归一值；
2. 存量行在迁移时被回填，且**每次启动都跟着归一表走**——改了楼盘归属之后，
   老房源不能一直挂在旧城市下（这种错在页面上看不出来）；
3. 单选（走 SQL）和多选（走 Python）用同一套判据，否则会出现「单选能查到、
   多选反而漏掉」。
"""
from __future__ import annotations

import sqlite3

import pytest

from models import Listing
from mstorage import Storage


def _listing(lid: str, city: str, source: str) -> Listing:
    return Listing(
        id=lid, name=f"unit {lid}", status="Available to book",
        price_raw="1000", available_from="", features=[], url="",
        city=city, source=source,
    )


def _reopen(path):
    """模拟进程重启后重新打开数据库。

    迁移按「路径 + 进程」只跑一次（``StorageBase._migrated_paths``），生产上
    这是对的——每次进程启动跑一遍即可；但用例要验证的正是「重启后回填」，
    必须先把这层缓存清掉。
    """
    from mstorage._base import StorageBase
    StorageBase._migrated_paths.clear()
    return Storage(path)


@pytest.fixture(autouse=True)
def _fresh_migration():
    from mstorage._base import StorageBase
    StorageBase._migrated_paths.clear()
    yield
    StorageBase._migrated_paths.clear()


@pytest.fixture
def db(tmp_path):
    st = Storage(tmp_path / "listings.db")
    yield st
    st.close()


class TestWritePath:
    def test_insert_stores_normalized_city(self, db):
        db.diff([_listing("x1", "Utrecht Willem Dreeslaan", "xior")])
        row = db.conn.execute(
            "SELECT city, city_normalized FROM listings WHERE id='x1'"
        ).fetchone()
        assert row["city"] == "Utrecht Willem Dreeslaan", "原值必须保留，页面还要展示"
        assert row["city_normalized"] == "Utrecht"

    def test_h2s_city_unchanged(self, db):
        db.diff([_listing("h1", "Eindhoven", "holland2stay")])
        row = db.conn.execute(
            "SELECT city_normalized FROM listings WHERE id='h1'"
        ).fetchone()
        assert row["city_normalized"] == "Eindhoven"


class TestBackfill:
    def _legacy_row(self, path, lid, city, source):
        """绕开 Storage 直接写，模拟归一列出现之前入库的老行。"""
        c = sqlite3.connect(path)
        c.execute(
            "UPDATE listings SET city_normalized='' WHERE id=?", (lid,)
        )
        c.commit()
        c.close()

    def test_existing_rows_are_backfilled(self, tmp_path):
        path = tmp_path / "listings.db"
        st = Storage(path)
        st.diff([_listing("x1", "Amsterdam Diemen", "ourdomain")])
        st.close()

        self._legacy_row(path, "x1", "Amsterdam Diemen", "ourdomain")

        st2 = _reopen(path)
        row = st2.conn.execute(
            "SELECT city_normalized FROM listings WHERE id='x1'"
        ).fetchone()
        st2.close()
        assert row["city_normalized"] == "Amsterdam", "存量行没有被回填"

    def test_backfill_follows_the_current_mapping(self, tmp_path, monkeypatch):
        """改了楼盘归属之后，存量行要跟着走，而不是停在建列那天的值。"""
        import config

        path = tmp_path / "listings.db"
        st = Storage(path)
        st.diff([_listing("x1", "Amsterdam Diemen", "ourdomain")])
        st.close()

        monkeypatch.setattr(
            config, "canonical_city",
            lambda v: "Diemen" if v == "Amsterdam Diemen" else v,
        )
        st2 = _reopen(path)
        row = st2.conn.execute(
            "SELECT city_normalized FROM listings WHERE id='x1'"
        ).fetchone()
        st2.close()
        assert row["city_normalized"] == "Diemen"


class TestQueries:
    @pytest.fixture
    def seeded(self, db):
        db.diff([
            _listing("h1", "Utrecht", "holland2stay"),
            _listing("h2", "Eindhoven", "holland2stay"),
            _listing("x1", "Utrecht Willem Dreeslaan", "xior"),
            _listing("o1", "Amsterdam Diemen", "ourdomain"),
            _listing("h3", "Amsterdam", "holland2stay"),
        ])
        return db

    def test_dropdown_shows_cities_not_buildings(self, seeded):
        assert seeded.get_distinct_cities() == [
            "Amsterdam", "Eindhoven", "Utrecht",
        ]

    def test_single_city_filter_spans_platforms(self, seeded):
        ids = {r["id"] for r in seeded.get_all_listings(city="Utrecht")}
        assert ids == {"h1", "x1"}, "勾 Utrecht 漏掉了 Xior 的楼盘"

    def test_filtering_by_a_building_name_still_works(self, seeded):
        """存量配置/书签里可能带着楼盘名，不能因此查不到东西。"""
        ids = {r["id"] for r in seeded.get_all_listings(city="Amsterdam Diemen")}
        assert ids == {"o1", "h3"}

    def test_multi_select_agrees_with_single_select(self, seeded, monkeypatch, tmp_path):
        """多选走 Python、单选走 SQL，两条路必须给出一致的结果。"""
        import app.db as app_db
        from app.services.listing_service import query_listing_rows

        # app.db 在模块导入时就绑定了 DB_PATH，改 config 上的那个没用
        monkeypatch.setattr(app_db, "DB_PATH", tmp_path / "listings.db")

        single = {r["id"] for r in query_listing_rows(cities=["Utrecht"])}
        multi = {
            r["id"] for r in query_listing_rows(cities=["Utrecht", "Eindhoven"])
        }
        assert single == {"h1", "x1"}
        assert single <= multi, "多选的结果没有包含单选的结果"
        assert multi == {"h1", "x1", "h2"}

    def test_multi_select_normalizes_the_user_side_too(
        self, seeded, monkeypatch, tmp_path
    ):
        """多选里出现楼盘名时也要归一。

        存量配置和收藏的链接里都可能带着「Amsterdam Diemen」。只归一房源侧
        的话，它在单选（走 SQL，两侧都归一）能查到，多选反而查不到。
        """
        import app.db as app_db
        from app.services.listing_service import query_listing_rows

        monkeypatch.setattr(app_db, "DB_PATH", tmp_path / "listings.db")

        ids = {
            r["id"] for r in
            query_listing_rows(cities=["Amsterdam Diemen", "Eindhoven"])
        }
        assert ids == {"o1", "h3", "h2"}

    def test_city_stats_span_platforms(self, seeded):
        assert seeded.count_all(city="Utrecht") == 2


class TestFallbackWhenNormalizedMissing:
    """归一值缺失时退回原始 city，而不是让这条房源整个消失。

    归一值由写入路径填、启动时回填。但只要将来有哪条写入路径漏了它，那条
    房源就会从所有城市筛选里查不到——不报错、不告警，页面上只是少了一条。
    退回原始 city 至少让它按字面值可查；对 H2S（占绝大多数）来说那本来就是
    正确的城市名。
    """

    def _insert_without_normalized(self, db, lid, city, source):
        db.conn.execute(
            """INSERT INTO listings
               (id, name, status, price_raw, available_from, features, url,
                city, first_seen, last_seen, notified, last_status, source)
               VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?)""",
            (lid, lid, "Available to book", "1000", "", "[]", "",
             city, "2026-01-01", "2026-01-01", "Available to book", source),
        )
        db.conn.commit()

    def test_row_is_still_findable(self, db):
        self._insert_without_normalized(db, "raw1", "Eindhoven", "holland2stay")
        ids = {r["id"] for r in db.get_all_listings(city="Eindhoven")}
        assert ids == {"raw1"}

    def test_row_still_appears_in_the_dropdown(self, db):
        self._insert_without_normalized(db, "raw1", "Eindhoven", "holland2stay")
        assert "Eindhoven" in db.get_distinct_cities()

    def test_counts_include_it(self, db):
        self._insert_without_normalized(db, "raw1", "Eindhoven", "holland2stay")
        assert db.count_all(city="Eindhoven") == 1
