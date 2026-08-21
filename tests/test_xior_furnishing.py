"""Xior 装修档位按楼/按房型判定。

背景
----
`SOURCE_ASSUMED_FEATURES["xior"]` 曾经写着 ``{"Finishing": "Furnished"}``，
按「整个 source 一个值」处理。2026-08-21 对着站点房型页逐栋核对，发现是错的
——装修档位**按 room type 变**，同一栋楼内部就分两档：

    Amsterdam Naritaweg    Deluxe / Unfurnished   站点无任何 furnished 项
    Amsterdam Naritaweg    Deluxe / Furnished     Fully furnished
    Aachen Vaals           Comfy                  Partially furnished
                                                  （"Bed frame - no mattress"）
    Amsterdam Karspeldreef Comfy                  Fully furnished

注意后两行：**同名的 "Comfy" 在两栋楼里不是一个档位**，房型名单独用不可靠。

生产库 69 条里 41 条被标错（3 条实为无家具、38 条实为半装），方向是错推：
勾 Furnished 的用户收到它们，勾 Unfurnished / Semi furnished 的反而收不到。
"""
from __future__ import annotations

import pytest

from config import SOURCE_ASSUMED_FEATURES, assumed_features
from scrapers.xior import (
    BUILDING_FURNISHING,
    XiorScraper,
    _to_listing,
    furnishing_for,
)


# ── 判定本身 ────────────────────────────────────────────────────


class TestFurnishingLookup:
    def test_floorplan_name_wins_over_building(self):
        """房型名后缀是上游针对**这一个房型**的声明，比整栋概括更准。"""
        # Naritaweg 刻意不登记整栋值，全靠房型名
        assert furnishing_for("p0196102", "Deluxe / Unfurnished") == "Unfurnished"
        assert furnishing_for("p0196102", "Deluxe / Furnished") == "Fully furnished"
        assert furnishing_for("p0196102", "Comfy / Unfurnished") == "Unfurnished"

    def test_name_suffix_beats_a_registered_building_too(self):
        """就算整栋登记了，房型名说了话也以房型名为准。"""
        assert BUILDING_FURNISHING["p0196062"] == "Fully furnished"
        assert furnishing_for("p0196062", "Comfy / Unfurnished") == "Unfurnished"

    def test_falls_back_to_the_building_registry(self):
        assert furnishing_for("p0196062", "Comfy") == "Fully furnished"
        assert furnishing_for("p0196503", "Essential") == "Fully furnished"

    def test_same_floorplan_name_differs_between_buildings(self):
        """这一条就是「不能只看房型名」的证据。"""
        assert furnishing_for("p0196062", "Comfy") == "Fully furnished"
        assert furnishing_for("p0196061", "Comfy") == "Semi furnished"

    def test_unknown_building_returns_none(self):
        """核不了就别猜。返回 None，调用方不写该字段。"""
        assert furnishing_for("p9999999", "Comfy") is None
        assert furnishing_for("", "") is None
        assert furnishing_for("p9999999", "") is None

    def test_partially_furnished_maps_to_semi_not_furnished(self):
        """Vaals 站点原文是 Partially furnished：床架有、床垫没有、无衣柜。

        按 models.py 的术语表这是 Semi furnished（荷兰语 gestoffeerd），
        标成 Furnished 就是把半装当全装推给用户。
        """
        assert furnishing_for("p0196061", "Comfy") == "Semi furnished"

    def test_values_come_from_the_canonical_vocabulary(self):
        """别新造词——过滤下拉是按库里出现过的值去重出来的。"""
        allowed = {"Fully furnished", "Semi furnished", "Unfurnished", "Furnished"}
        assert set(BUILDING_FURNISHING.values()) <= allowed


# ── 与 source 级假设的关系 ──────────────────────────────────────


class TestSourceAssumptionNoLongerClaimsFurnishing:
    def test_xior_no_longer_assumes_furnished(self):
        assert "Finishing" not in SOURCE_ASSUMED_FEATURES["xior"], (
            "整个 source 一个装修档位是错的——同一栋楼内部就分两档"
        )

    def test_tenant_assumption_survives(self):
        """Xior 是纯学生盘，这条整源假设是对的，不要连坐删掉。"""
        assert SOURCE_ASSUMED_FEATURES["xior"]["Tenant"] == "student only"
        assert assumed_features("xior") == ["Tenant: student only"]

    def test_ourdomain_assumption_untouched(self):
        assert SOURCE_ASSUMED_FEATURES["ourdomain"]["Finishing"] == "Furnished"


# ── 落到 Listing 上 ─────────────────────────────────────────────


def _unit(**kw):
    u = {"apartmentId": "1", "apartmentName": "101", "floorplanName": "Comfy",
         "sqm": 22, "minimumRent": 500, "maximumRent": 500}
    u.update(kw)
    return u


def _feats(building_key, fp_name):
    lst = _to_listing(_unit(floorplanName=fp_name), display="X",
                      building_url="", building_key=building_key)
    return lst.features


class TestListingFeatures:
    def test_registered_building_gets_a_finishing_feature(self):
        assert "Finishing: Fully furnished" in _feats("p0196062", "Comfy")

    def test_unfurnished_floorplan_says_so(self):
        f = _feats("p0196102", "Deluxe / Unfurnished")
        assert "Finishing: Unfurnished" in f
        assert "Finishing: Furnished" not in f
        assert "Finishing: Fully furnished" not in f

    def test_unknown_building_omits_the_field(self):
        """写不出就不写。finishing 对 xior 是 fail-closed，缺值即被筛掉——
        宁可少推，不可把半装/无家具的房源当全装推出去。"""
        assert not [x for x in _feats("p9999999", "Comfy") if x.startswith("Finishing:")]

    def test_tenant_is_still_on_every_listing(self):
        assert "Tenant: student only" in _feats("p9999999", "Comfy")


# ── 登记表本身 ──────────────────────────────────────────────────


class TestRegistryHygiene:
    def test_every_registered_key_is_a_real_building(self):
        unknown = sorted(set(BUILDING_FURNISHING) - set(XiorScraper.BUILDINGS))
        assert not unknown, f"登记了不存在的楼栋 key: {unknown}"

    def test_naritaweg_is_deliberately_absent(self):
        """它四个房型分两档，登记一个整栋值会盖掉正确答案。"""
        assert "p0196102" not in BUILDING_FURNISHING

    def test_finishing_stays_a_registered_filter_dim(self):
        """摘掉的是整源假设，不是过滤能力。

        若把 finishing 从 xior 的维度表里摘掉，它会变成 fail-open——已知是
        Unfurnished 的房源也会在勾 Furnished 时被放行，比改动前更糟。
        """
        from config import source_supports_dim
        assert source_supports_dim("xior", "finishing")


# ── 存量订正 ────────────────────────────────────────────────────


class TestResyncExistingRows:
    """``_backfill_assumed_features`` 只补不改，存量错值靠它清不掉。

    2026-08-21 之前所有 Xior 房源都被写成 ``Finishing: Furnished``，生产库 69 条
    里 41 条是错的。它们绝大多数是 Occupied、不会再被 feed 返回，diff() 的
    UPDATE 也等不到——必须有一趟覆盖写。
    """

    @pytest.fixture(autouse=True)
    def _fresh(self):
        from mstorage._base import StorageBase
        StorageBase._migrated_paths.clear()
        yield
        StorageBase._migrated_paths.clear()

    def _seed(self, path, feats):
        import json
        from mstorage import Storage
        st = Storage(path)
        st.conn.execute(
            """INSERT INTO listings (id, name, status, price_raw, available_from,
                   features, url, city, first_seen, last_seen, notified,
                   last_status, source)
               VALUES ('x1','x','Occupied','1000','', ?, '', 'Amsterdam',
                       '2026-01-01','2026-01-01',1,'Occupied','xior')""",
            (json.dumps(feats, ensure_ascii=False),),
        )
        st.conn.commit()
        st.close()

    def _reopen_feats(self, path):
        import json
        from mstorage._base import StorageBase
        from mstorage import Storage
        StorageBase._migrated_paths.clear()
        st = Storage(path)
        row = st.conn.execute("SELECT features FROM listings WHERE id='x1'").fetchone()
        st.close()
        return json.loads(row["features"])

    def test_wrong_furnished_becomes_unfurnished(self, tmp_path):
        """3 条真实存量长这样：房型名写着 Unfurnished，字段却标 Furnished。"""
        p = tmp_path / "listings.db"
        self._seed(p, ["Unit: 145L", "Building: Amsterdam Naritaweg",
                       "Finishing: Furnished", "Floorplan: Deluxe / Unfurnished"])
        f = self._reopen_feats(p)
        assert "Finishing: Unfurnished" in f
        assert "Finishing: Furnished" not in f

    def test_wrong_furnished_becomes_semi(self, tmp_path):
        """38 条真实存量长这样：Vaals 站点写的是 Partially furnished。"""
        p = tmp_path / "listings.db"
        self._seed(p, ["Unit: A", "Building: Aachen Vaals Katzensprung",
                       "Finishing: Furnished", "Floorplan: Comfy"])
        f = self._reopen_feats(p)
        assert "Finishing: Semi furnished" in f
        assert "Finishing: Furnished" not in f

    def test_correct_rows_are_upgraded_to_the_precise_value(self, tmp_path):
        p = tmp_path / "listings.db"
        self._seed(p, ["Unit: A", "Building: Amsterdam Karspeldreef",
                       "Finishing: Furnished", "Floorplan: Comfy"])
        assert "Finishing: Fully furnished" in self._reopen_feats(p)

    def test_unknown_building_loses_the_bogus_value(self, tmp_path):
        """判不出就该没有，而不是留着一个猜的值。"""
        p = tmp_path / "listings.db"
        self._seed(p, ["Unit: A", "Building: Somewhere New",
                       "Finishing: Furnished", "Floorplan: Comfy"])
        f = self._reopen_feats(p)
        assert not [x for x in f if x.startswith("Finishing:")]
        assert "Unit: A" in f

    def test_other_features_are_preserved(self, tmp_path):
        p = tmp_path / "listings.db"
        self._seed(p, ["Unit: A", "Building: Amsterdam Karspeldreef",
                       "Tenant: student only", "Finishing: Furnished",
                       "Floorplan: Comfy", "Area: 21 m²", "Deposit: €0"])
        f = self._reopen_feats(p)
        for keep in ("Unit: A", "Tenant: student only", "Floorplan: Comfy",
                     "Area: 21 m²", "Deposit: €0"):
            assert keep in f

    def test_is_idempotent(self, tmp_path):
        """每次启动都跑，不打一次性标记——所以必须幂等。"""
        p = tmp_path / "listings.db"
        self._seed(p, ["Unit: A", "Building: Aachen Vaals Katzensprung",
                       "Finishing: Furnished", "Floorplan: Comfy"])
        first = self._reopen_feats(p)
        assert self._reopen_feats(p) == first
        assert [x for x in first if x.startswith("Finishing:")] == [
            "Finishing: Semi furnished"]

    def test_only_touches_xior(self, tmp_path):
        """别的 source 的 Finishing 可能是真抓来的，不许动。"""
        import json
        from mstorage import Storage
        from mstorage._base import StorageBase
        p = tmp_path / "listings.db"
        st = Storage(p)
        st.conn.execute(
            """INSERT INTO listings (id, name, status, price_raw, available_from,
                   features, url, city, first_seen, last_seen, notified,
                   last_status, source)
               VALUES ('h1','h','Occupied','1000','',
                       '["Finishing: Kaal"]','','Eindhoven',
                       '2026-01-01','2026-01-01',1,'Occupied','holland2stay')"""
        )
        st.conn.commit(); st.close()
        StorageBase._migrated_paths.clear()
        st2 = Storage(p)
        row = st2.conn.execute("SELECT features FROM listings WHERE id='h1'").fetchone()
        st2.close()
        assert "Finishing: Kaal" in json.loads(row["features"])


class TestScrapeWiresTheBuildingKey:
    """``scrape()` 必须把楼栋 key 交给 ``_to_listing``。

    上面那些用例都是直接调 ``_to_listing`` 并显式传 key，所以**删掉调用点的
    ``building_key=`` 参数照样全绿**——而生产里那样一改，每条房源的档位都会
    退化成 None（默认空串查不到任何楼），装修筛选对整个 Xior 静默失效。

    所以这一条走 ``scrape()`` 全链路，让接线本身被钉住。
    """

    def _scrape(self, monkeypatch, city_key, fp_name):
        from scrapers import xior as x
        from scrapers.base import ScrapeTask

        # 不然每个 room_type 之间要真睡 5 秒（Naritaweg 4 个 = 20 秒）
        monkeypatch.setattr(x, "_MIN_REQUEST_INTERVAL", 0.0)
        monkeypatch.setattr(x, "_post_ajax", lambda *a, **k: {
            "units": [{
                "apartmentId": "1", "apartmentName": "101",
                "floorplanName": fp_name, "sqm": 22,
                "minimumRent": 500, "maximumRent": 500,
            }],
            "availability_response": {"errorCode": 200},
        })
        s = x.XiorScraper()
        s._fetcher = object()                      # 跳过建浏览器
        monkeypatch.setattr(s, "_verify_bookable_floorplans", lambda *a, **k: None)
        bldg = x.XiorScraper.BUILDINGS[city_key]
        task = ScrapeTask(source="xior", city_key=city_key,
                          city_display=bldg["display"])
        return s.scrape(task).listings

    def test_registered_building_reaches_the_listing(self, monkeypatch):
        got = self._scrape(monkeypatch, "p0196061", "Comfy")     # Vaals
        assert got and "Finishing: Semi furnished" in got[0].features, (
            "楼栋 key 没传到 _to_listing——装修档位对整个 Xior 静默失效"
        )

    def test_floorplan_suffix_reaches_the_listing(self, monkeypatch):
        got = self._scrape(monkeypatch, "p0196102", "Deluxe / Unfurnished")
        assert got and "Finishing: Unfurnished" in got[0].features
