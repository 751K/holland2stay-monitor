"""浏览页的筛选与展示：与通知那条路共用判据，展示值也归一。

三件事在 2026-08-05 打开界面时才发现：

1. 浏览页有**第二套匹配实现**（``feature_contains`` 是另写的裸子串）。修好通知
   侧的荷兰语归一与装修分档之后，页面仍按老规矩走——勾「装修 = Furnished」
   页面回 251 条（含 Semi / Fully / Unfurnished），通知只发 187 条。同一个条件
   两个答案。
2. 装修是单选。四档互斥意味着一次只能看一档，想看「有家具或全装修」得查两次。
3. 筛选下拉写 ``Two (only couples)``，房源卡片上却是 ``Twee (alleen koppels)``
   ——归一只做在筛选侧，展示侧没做，用户会怀疑筛选把这条漏了。
"""
from __future__ import annotations

import json

import pytest

from app.services.listing_service import (
    feature_contains,
    normalize_listing_row,
    query_listing_rows,
)
from config import assumed_features
from models import Listing
from mstorage import Storage


def _row(lid: str, source: str = "holland2stay", **features) -> dict:
    return {
        "id": lid, "name": lid, "status": "Available to book",
        "price_raw": "€1000", "available_from": "", "url": "",
        "city": "Eindhoven", "source": source,
        "features": json.dumps([f"{k}: {v}" for k, v in features.items()],
                               ensure_ascii=False),
    }


class TestFeatureContainsSharesTheJudgement:
    """浏览页与通知必须用同一套判据，否则同一个条件两个答案。"""

    def test_finishing_tiers_are_exclusive_here_too(self):
        row = _row("a", Finishing="Unfurnished")
        assert not feature_contains(row, "Finishing", "Furnished"), \
            "浏览页把反义词也收了进来"
        assert not feature_contains(_row("b", Finishing="Semi furnished"),
                                    "Finishing", "Furnished")
        assert not feature_contains(_row("c", Finishing="Fully furnished"),
                                    "Finishing", "Furnished")
        assert feature_contains(_row("d", Finishing="Furnished"),
                                "Finishing", "Furnished")

    def test_dutch_values_match(self):
        assert feature_contains(_row("a", Finishing="Gemeubileerd"),
                                "Finishing", "Furnished")
        assert feature_contains(_row("b", Occupancy="Twee (alleen koppels)"),
                                "Occupancy", "Two (only couples)")
        assert feature_contains(_row("c", Type="Loft (open slaapkamer)"),
                                "Type", "Loft (open bedroom area)")

    def test_cross_platform_type_wording_still_matches(self):
        """房型在 H2S 写 1、在 OurDomain 写 1-Bedroom Apartment，不能整体相等。"""
        assert feature_contains(_row("a", Type="1-Bedroom Apartment"), "Type", "1")

    def test_unknown_category_falls_back_to_word_boundary(self):
        """没登记进维度表的类目走词边界，不是整体相等，也不是裸子串。"""
        assert feature_contains(_row("a", Building="Zernike Tower"),
                                "Building", "Zernike")
        assert not feature_contains(_row("b", Building="Zernikestraat"),
                                    "Building", "Zernike")


class TestFinishingIsMultiSelect:
    @pytest.fixture
    def db(self, tmp_path, monkeypatch):
        import app.db as app_db

        st = Storage(tmp_path / "listings.db")
        st.diff([
            Listing(id=f"L{i}", name=f"L{i}", status="Available to book",
                    price_raw="1000", available_from="", url="",
                    city="Eindhoven", source="holland2stay",
                    features=[f"Finishing: {v}"])
            for i, v in enumerate(
                ["Unfurnished", "Semi furnished", "Furnished", "Fully furnished",
                 "Gemeubileerd"])
        ])
        st.close()
        monkeypatch.setattr(app_db, "DB_PATH", tmp_path / "listings.db")
        yield

    def test_single_value_still_works(self, db):
        """API 与旧链接可能只带一个值，不能因为改成多选就断掉。"""
        ids = {r["id"] for r in query_listing_rows(finishing="Furnished")}
        assert ids == {"L2", "L4"}, "L4 是 Gemeubileerd，归一后属于 Furnished"

    def test_multiple_values_are_or(self, db):
        ids = {r["id"] for r in query_listing_rows(
            finishing=["Furnished", "Fully furnished"])}
        assert ids == {"L2", "L3", "L4"}

    def test_each_tier_is_disjoint(self, db):
        seen = set()
        for tier in ("Unfurnished", "Semi furnished", "Furnished", "Fully furnished"):
            ids = {r["id"] for r in query_listing_rows(finishing=[tier])}
            assert not (ids & seen), f"{tier} 与前面的档位有重叠"
            seen |= ids
        assert len(seen) == 5, "四档相加应覆盖全部房源，不重不漏"

    def test_empty_list_means_no_filter(self, db):
        assert len(query_listing_rows(finishing=[])) == 5


class TestBrowsePageAgreesWithNotifications:
    """平台不提供某维度时，浏览页也要 fail-open。

    此前浏览页没有这一层：一条没有该字段的房源在那里必然不匹配，而通知侧放行。
    同一个条件两个答案，线上实测差值恒定 83——正好是当时没有 Finishing 字段的
    房源数（Xior 66 + OurDomain 17）。

    装修那一例后来换了更好的解法：它们的档位由 SOURCE_ASSUMED_FEATURES 直接声明，
    不再依赖 fail-open（见 test_assumed_features.py）。fail-open 仍然管着它们真正
    没有的维度——合同 / 租客 / 优惠 / 能耗 / 片区。
    """

    @pytest.fixture
    def db(self, tmp_path, monkeypatch):
        import app.db as app_db

        st = Storage(tmp_path / "listings.db")
        st.diff([
            Listing(id="H_furn", name="h", status="Available to book",
                    price_raw="1000", available_from="", url="", city="Eindhoven",
                    source="holland2stay", features=["Finishing: Furnished"]),
            Listing(id="H_unfurn", name="h", status="Available to book",
                    price_raw="1000", available_from="", url="", city="Eindhoven",
                    source="holland2stay", features=["Finishing: Unfurnished"]),
            # Xior / OurDomain 的 feed 里没有 Finishing，装修档位由
            # SOURCE_ASSUMED_FEATURES 声明，scraper 组装 Listing 时带上——
            # 这里照同一条路构造，否则测的是生产中不存在的形态。
            # 合同 / 租客 / 能耗仍是它们真正没有的维度。
            Listing(id="X1", name="x", status="Available to book",
                    price_raw="1000", available_from="", url="", city="Utrecht",
                    source="xior",
                    features=["Unit: 1.S127", *assumed_features("xior")]),
            Listing(id="O1", name="o", status="Available to book",
                    price_raw="1000", available_from="", url="", city="Amsterdam",
                    source="ourdomain",
                    features=["Type: Studio", *assumed_features("ourdomain")]),
        ])
        st.close()
        monkeypatch.setattr(app_db, "DB_PATH", tmp_path / "listings.db")
        yield

    def test_unsupported_platforms_pass_through(self, db):
        # 合同是 H2S 独有；装修不能再拿来举例——Xior / OurDomain 现在有声明值。
        ids = {r["id"] for r in query_listing_rows(contract="Indefinite")}
        assert {"X1", "O1"} <= ids, \
            "Xior / OurDomain 因为 feed 里没这个字段被整个排除了"

    def test_supported_platform_is_still_filtered(self, db):
        ids = {r["id"] for r in query_listing_rows(finishing=["Furnished"])}
        assert "H_unfurn" not in ids, "支持该维度的平台不该放宽"

    def test_declared_platforms_land_in_the_right_tier(self, db):
        """Xior / OurDomain 有声明值，勾 Unfurnished 时不该出现。"""
        furn = {r["id"] for r in query_listing_rows(finishing=["Furnished"])}
        unfurn = {r["id"] for r in query_listing_rows(finishing=["Unfurnished"])}
        assert {"X1", "O1"} <= furn
        assert not ({"X1", "O1"} & unfurn), "声明是 Furnished，却出现在无家具里"

    def test_matches_the_notification_path(self, db):
        """两条路必须给出同一个集合，这是本次修复的全部意义。"""
        from config import ListingFilter
        from app.services.listing_service import row_to_listing, storage_ctx

        lf = ListingFilter(allowed_finishing=["Furnished"])
        with storage_ctx() as st:
            rows = st.get_all_listings(limit=100)
        notif = {r["id"] for r in rows if lf.passes(row_to_listing(r))}
        page = {r["id"] for r in query_listing_rows(finishing=["Furnished"])}
        assert notif == page, f"通知 {sorted(notif)} ≠ 页面 {sorted(page)}"

    def test_type_follows_the_capability_table(self, db):
        """OurDomain 提供房型、Xior 不提供——不能一刀切。"""
        ids = {r["id"] for r in query_listing_rows(types=["Studio"])}
        assert "O1" in ids, "OurDomain 有房型，Studio 应命中"
        assert "X1" in ids, "Xior 不提供房型，该条件对它应跳过"
        assert "H_furn" not in ids, "H2S 提供房型但这条没有 Studio"


class TestDisplayNormalization:
    def test_controlled_values_are_canonicalized(self):
        out = normalize_listing_row(_row(
            "a", Occupancy="Twee (alleen koppels)", Finishing="Gemeubileerd",
            Type="Loft (open slaapkamer)", Contract="Onbepaalde tijd",
        ))
        feats = json.loads(out["features"])
        assert "Occupancy: Two (only couples)" in feats
        assert "Finishing: Furnished" in feats
        assert "Type: Loft (open bedroom area)" in feats
        assert "Contract: Indefinite" in feats

    def test_category_names_are_untouched(self):
        out = normalize_listing_row(_row("a", Occupancy="Twee personen"))
        assert json.loads(out["features"])[0].startswith("Occupancy: ")

    def test_free_text_categories_are_left_alone(self):
        """同义表按整个值查，扫过自由文本会误伤。

        某个片区若恰好叫 ``Kaal``，会被改写成 ``Unfurnished``；楼盘名、门牌同理。
        """
        out = normalize_listing_row(_row(
            "a", Neighborhood="Kaal", Building="Gemeubileerd", Unit="#590"))
        feats = json.loads(out["features"])
        assert "Neighborhood: Kaal" in feats
        assert "Building: Gemeubileerd" in feats
        assert "Unit: #590" in feats

    def test_database_value_is_not_mutated(self):
        row = _row("a", Occupancy="Twee (alleen koppels)")
        original = row["features"]
        normalize_listing_row(row)
        assert row["features"] == original, "归一只该作用在展示副本上"

    def test_malformed_features_survive(self):
        row = {"id": "a", "features": "not json", "source": "holland2stay"}
        assert normalize_listing_row(row)["features"] in ("not json", "[]")

    def test_items_without_a_category_pass_through(self):
        row = {"id": "a", "source": "holland2stay",
               "features": json.dumps(["裸字符串", "Occupancy: Twee personen"])}
        feats = json.loads(normalize_listing_row(row)["features"])
        assert feats[0] == "裸字符串"
        assert feats[1] == "Occupancy: Two"
