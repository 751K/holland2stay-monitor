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

from config import ListingFilter, canonical_city, known_city_names
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


# ── 归一规则本身与通知/筛选判据（原 test_city_normalization.py） ────
#
# `city` 这一列在四个平台上存的不是同一种东西。
#
# H2S 存真城市（Eindhoven），Xior / OurDomain / OurCampus 存楼盘名
# （Utrecht Willem Dreeslaan、Amsterdam Diemen）。而 `allowed_cities` 是精确
# 匹配，于是勾了「Utrecht」的用户永远收不到 Xior 在 Utrecht 那 25 套房的通知
# ——数据在库里、平台也勾了、抓取一切正常，面板上看不出任何异常。
#
# 2026-08-05 查线上：14 个用户受影响，累计 56 条房源本该通知而没有通知。
#
# 修法是加一层归一：原始值照常保留展示，筛选走归一后的城市。

class TestCanonicalCity:
    @pytest.mark.parametrize("raw,want", [
        # Xior：config 里本来就有 city/bldg 拆分，直接用
        ("Utrecht Willem Dreeslaan", "Utrecht"),
        ("Eindhoven Zernikestraat", "Eindhoven"),
        ("Amsterdam Naritaweg", "Amsterdam"),
        ("Groningen Eendrachtskade", "Groningen"),
        # OurDomain / OurCampus
        ("Amsterdam Diemen", "Amsterdam"),
        ("Amsterdam South-East", "Amsterdam"),
        ("OurCampus Amsterdam Diemen", "Amsterdam"),
        # H2S 本来就是城市名，原样通过
        ("Eindhoven", "Eindhoven"),
        ("The Hague", "The Hague"),
    ])
    def test_known_locations(self, raw, want):
        assert canonical_city(raw) == want

    def test_prefix_guessing_would_be_wrong(self):
        """不能靠前缀猜城市。

        「Aachen Vaals Katzensprung」的城市是「Aachen Vaals」不是「Aachen」，
        猜一次错一次；猜错会把房源归到一个不存在的城市，比不归一还糟。
        """
        assert canonical_city("Aachen Vaals Katzensprung") == "Aachen Vaals"

    @pytest.mark.parametrize("raw", ["", "   "])
    def test_blank_stays_blank(self, raw):
        assert canonical_city(raw) == ""

    def test_unknown_value_passes_through(self):
        """没收录的新楼盘原样返回——宁可维持现状，也不要猜出一个错误的城市。"""
        assert canonical_city("Rotterdam Nieuwe Toren") == "Rotterdam Nieuwe Toren"

    def test_case_and_whitespace_insensitive(self):
        assert canonical_city("  utrecht willem dreeslaan  ") == "Utrecht"

    def test_h2s_city_that_is_also_a_prefix_is_untouched(self):
        """Diemen 是 H2S 的独立城市，不能被 Amsterdam Diemen 的映射带走。"""
        assert canonical_city("Diemen") == "Diemen"


class TestKnownCityNames:
    def test_includes_xior_only_cities(self):
        """筛选下拉必须覆盖全平台。

        原先只用 KNOWN_CITIES（H2S 的 26 个），Xior 独有的 Wageningen /
        Venlo / Breda / Leeuwarden 根本不在选项里——用户既选不到，一旦设了
        城市筛选，这些楼盘的房源就被整体挡掉。
        """
        names = set(known_city_names())
        for city in ("Wageningen", "Venlo", "Breda", "Leeuwarden"):
            assert city in names, f"{city} 不在城市选项里"

    def test_contains_no_building_names(self):
        names = known_city_names()
        for n in names:
            assert canonical_city(n) == n, f"{n!r} 是楼盘名，不该出现在城市选项里"

    def test_sorted_and_deduped(self):
        names = known_city_names()
        assert names == sorted(set(names))


def _city_listing(city: str, source: str) -> Listing:
    return Listing(
        id=f"{source}-{city}", name="x", status="Available to book",
        price_raw="1000", available_from="", features=[], url="",
        city=city, source=source,
    )


class TestNotificationFilter:
    """这条路管的是发不发通知，比列表页严重。"""

    def test_city_matches_building_in_that_city(self):
        f = ListingFilter(allowed_cities=["Utrecht"])
        assert f.passes(_city_listing("Utrecht Willem Dreeslaan", "xior")), \
            "勾了 Utrecht 仍然收不到 Utrecht 的 Xior 房源"

    def test_amsterdam_matches_ourdomain_buildings(self):
        """线上 Yixin 的实际情形：勾 Amsterdam，17 条 OurDomain 房源收不到。"""
        f = ListingFilter(allowed_cities=["Amsterdam"])
        assert f.passes(_city_listing("Amsterdam Diemen", "ourdomain"))
        assert f.passes(_city_listing("Amsterdam South-East", "ourdomain"))
        assert f.passes(_city_listing("Amsterdam Naritaweg", "xior"))

    def test_stored_building_name_still_works(self):
        """存量配置里存的是楼盘名，不能因为改了判据就失效。"""
        f = ListingFilter(allowed_cities=["Amsterdam Diemen"])
        assert f.passes(_city_listing("Amsterdam Diemen", "ourdomain"))
        # 归一之后它等价于 Amsterdam，同城的其它楼盘也一并放行
        assert f.passes(_city_listing("Amsterdam", "holland2stay"))

    def test_other_cities_still_excluded(self):
        """归一是把同城的合并，不是把所有城市合并。"""
        f = ListingFilter(allowed_cities=["Utrecht"])
        assert not f.passes(_city_listing("Eindhoven", "holland2stay"))
        assert not f.passes(_city_listing("Eindhoven Zernikestraat", "xior"))

    def test_diemen_does_not_leak_into_amsterdam(self):
        """勾 Diemen（H2S 的独立城市）不该收到 Amsterdam 的房。"""
        f = ListingFilter(allowed_cities=["Diemen"])
        assert f.passes(_city_listing("Diemen", "holland2stay"))
        assert not f.passes(_city_listing("Amsterdam", "holland2stay"))

    def test_empty_filter_unaffected(self):
        assert ListingFilter().passes(_city_listing("Amsterdam Diemen", "ourdomain"))
