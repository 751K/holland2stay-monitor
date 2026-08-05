"""平台整体成立、feed 里不上报的属性，直接声明出来。

Xior 与 OurDomain 的房源全部带家具，也就是 H2S 口径下的 ``Furnished``；它们的
feed 只是没有这个字段。此前靠 fail-open 兜底——「平台不提供该维度就整体放行」
——那条规则不区分用户勾的是哪一档：

    修前   勾 Furnished    → 出现（对，但理由是「缺字段」而不是「真的是」）
           勾 Unfurnished  → 也出现（错，它们恰恰不是无家具的）

把事实写进 ``SOURCE_ASSUMED_FEATURES`` 之后 fail-open 对该维度不再需要，两个方向
都对。生产快照实测：``Unfurnished`` 从 86 条回到 3 条，``Furnished`` 270 条，
四档相加 390 = 全库。
"""
from __future__ import annotations

import json

import pytest

from config import (
    SOURCE_ASSUMED_FEATURES,
    ListingFilter,
    assumed_features,
    sources_supporting_dim,
)
from models import Listing
from mstorage import Storage


class TestDeclaration:
    def test_xior_and_ourdomain_are_furnished(self):
        assert assumed_features("xior") == ["Finishing: Furnished"]
        assert assumed_features("ourdomain") == ["Finishing: Furnished"]

    def test_ourcampus_is_not_declared(self):
        """OurCampus 至今没返回过任何单元，装修档位无从核实。

        它复用 OurDomain 的解析器，很容易顺手一起写上——但那等于登记一个没验证
        过的事实。宁可让它继续走 fail-open。
        """
        assert assumed_features("ourcampus") == []
        assert "ourcampus" not in sources_supporting_dim("finishing")

    def test_h2s_declares_nothing(self):
        """H2S 自己上报装修档位，不能拿声明去覆盖抓来的数据。"""
        assert assumed_features("holland2stay") == []

    def test_capability_table_matches_the_declaration(self):
        """声明了属性，能力表就得跟着登记——否则该维度仍走 fail-open，
        勾 Unfurnished 时这些房源照样会出现。"""
        for source, feats in SOURCE_ASSUMED_FEATURES.items():
            for cat in feats:
                dim = cat.lower()
                assert source in sources_supporting_dim(dim), \
                    f"{source} 声明了 {cat} 却没登记进能力表"


def _listing(lid: str, source: str, features=()) -> Listing:
    from config import assumed_features as af
    return Listing(
        id=lid, name=lid, status="Available to book", price_raw="1000",
        available_from="", url="", city="Utrecht", source=source,
        features=[*features, *af(source)],
    )


class TestFilterBehaviour:
    def test_furnished_matches(self):
        f = ListingFilter(allowed_finishing=["Furnished"])
        assert f.passes(_listing("x", "xior"))
        assert f.passes(_listing("o", "ourdomain"))

    def test_unfurnished_no_longer_matches(self):
        """这是本次修复的重点：它们不是无家具的。"""
        f = ListingFilter(allowed_finishing=["Unfurnished"])
        assert not f.passes(_listing("x", "xior"))
        assert not f.passes(_listing("o", "ourdomain"))

    def test_other_tiers_do_not_match(self):
        for tier in ("Semi furnished", "Fully furnished"):
            f = ListingFilter(allowed_finishing=[tier])
            assert not f.passes(_listing("x", "xior")), tier

    def test_ourcampus_still_fails_open(self):
        """没声明的平台维持原样：条件对它整体跳过。"""
        for tier in ("Furnished", "Unfurnished"):
            f = ListingFilter(allowed_finishing=[tier])
            assert f.passes(_listing("c", "ourcampus"))


class TestScrapersEmitIt:
    """声明写在 config 里没用，解析函数得真的把它拼进去。

    只测「手工拼的 Listing 带着这个字段」等于什么都没测——那是测试自己拼的。
    """

    def test_xior(self):
        from datetime import date

        from scrapers.xior import _to_listing

        l = _to_listing(
            {"apartmentId": "1", "apartmentName": "1.S127", "sqm": 22,
             "minimumRent": 800, "availableDate": "2026-09-01"},
            display="Utrecht Willem Dreeslaan",
            building_url="https://x.test/",
            today=date(2026, 8, 5),
        )
        assert "Finishing: Furnished" in l.features

    def test_ourdomain(self):
        from scrapers.ourdomain import _to_listing

        l = _to_listing(
            {"unit_id": "999", "apt": "#A1", "sqft": "22", "rent": "€ 1.500",
             "deposit": "€ 0", "detail": "", "floor": 0,
             "status": "Available to book", "avail_date": "2026-09-01",
             "fp_ids": []},
            base_url="https://x.test/fp.aspx",
            city_display="Amsterdam Diemen",
            source="ourdomain",
        )
        assert "Finishing: Furnished" in l.features

    def test_ourcampus_reuses_the_parser_but_gets_nothing(self):
        """同一个解析函数按 source 取声明，不能写死成 OurDomain 那份。"""
        from scrapers.ourdomain import _to_listing

        l = _to_listing(
            {"unit_id": "999", "apt": "#A1", "sqft": "22", "rent": "€ 1.500",
             "deposit": "€ 0", "detail": "", "floor": 0,
             "status": "Available to book", "avail_date": "2026-09-01",
             "fp_ids": []},
            base_url="https://x.test/fp.aspx",
            city_display="OurCampus Amsterdam Diemen",
            source="ourcampus",
            id_prefix="oc_",
        )
        assert not any(f.startswith("Finishing: ") for f in l.features)


class TestBackfill:
    """存量房源里绝大多数是 Occupied，不会再被 feed 返回。

    靠 ``diff()`` 的 UPDATE 永远等不到它们，而它们仍然出现在浏览页里——缺了这个
    字段就筛不出来。
    """

    def _reopen(self, path):
        from mstorage._base import StorageBase
        StorageBase._migrated_paths.clear()
        return Storage(path)

    @pytest.fixture(autouse=True)
    def _fresh(self):
        from mstorage._base import StorageBase
        StorageBase._migrated_paths.clear()
        yield
        StorageBase._migrated_paths.clear()

    def _feats(self, st, lid):
        row = st.conn.execute(
            "SELECT features FROM listings WHERE id=?", (lid,)
        ).fetchone()
        return json.loads(row["features"])

    def test_existing_rows_get_the_feature(self, tmp_path):
        path = tmp_path / "listings.db"
        st = Storage(path)
        # 模拟声明出现之前入库的行
        st.conn.execute(
            """INSERT INTO listings (id, name, status, price_raw, available_from,
                   features, url, city, first_seen, last_seen, notified,
                   last_status, source)
               VALUES ('x1','x','Occupied','1000','', '["Unit: 1.S127"]','',
                       'Utrecht','2026-01-01','2026-01-01',0,'Occupied','xior')"""
        )
        st.conn.commit()
        st.close()

        st2 = self._reopen(path)
        assert "Finishing: Furnished" in self._feats(st2, "x1")
        st2.close()

    def test_scraped_value_wins(self, tmp_path):
        """上游哪天真的开始上报了，抓到的值优先，不能被声明覆盖。"""
        path = tmp_path / "listings.db"
        st = Storage(path)
        st.conn.execute(
            """INSERT INTO listings (id, name, status, price_raw, available_from,
                   features, url, city, first_seen, last_seen, notified,
                   last_status, source)
               VALUES ('x1','x','Occupied','1000','',
                       '["Finishing: Semi furnished"]','','Utrecht',
                       '2026-01-01','2026-01-01',0,'Occupied','xior')"""
        )
        st.conn.commit()
        st.close()

        st2 = self._reopen(path)
        feats = self._feats(st2, "x1")
        st2.close()
        assert feats == ["Finishing: Semi furnished"]

    def test_undeclared_sources_untouched(self, tmp_path):
        path = tmp_path / "listings.db"
        st = Storage(path)
        st.diff([Listing(id="h1", name="h", status="Available to book",
                         price_raw="1000", available_from="", url="",
                         city="Eindhoven", source="holland2stay", features=[])])
        st.close()

        st2 = self._reopen(path)
        assert self._feats(st2, "h1") == []
        st2.close()

    def test_repeated_runs_do_not_duplicate(self, tmp_path):
        path = tmp_path / "listings.db"
        st = Storage(path)
        st.diff([Listing(id="x1", name="x", status="Available to book",
                         price_raw="1000", available_from="", url="",
                         city="Utrecht", source="xior", features=["Unit: A"])])
        st.close()

        for _ in range(3):
            st2 = self._reopen(path)
            feats = self._feats(st2, "x1")
            st2.close()
        assert feats.count("Finishing: Furnished") == 1
