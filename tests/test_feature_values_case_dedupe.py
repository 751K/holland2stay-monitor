"""``get_feature_values`` 的大小写去重。

magis 的房型写 ``studio``，另外四个平台写 ``Studio``。``canonical_feature``
只归一收录过的同义词，大小写不同的同一个词原样放行，于是筛选页的 Types 下拉
里并排出现两个字面完全一样的 "Studio"——看着像界面渲染重复了。

过滤结果不受影响：``whitelist_matches`` 两个分支都大小写不敏感，勾哪一个都
命中全部房源。坏的只是候选列表本身。这组测试守住候选列表。
"""
import json

import pytest

from mstorage._listings import _prefer_spelling, dedupe_feature_values


def _add(st, listing_id: str, source: str, features: list[str]) -> None:
    st._conn.execute(
        "INSERT OR REPLACE INTO listings (id, name, status, price_raw, "
        "available_from, features, url, city, source) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (listing_id, listing_id, "Available to book", "€700", "2026-06-01",
         json.dumps(features), f"https://x/{listing_id}", "Eindhoven", source),
    )
    st._conn.commit()


class TestCaseDedupe:
    def test_case_variants_collapse_to_one_option(self, temp_db):
        _add(temp_db, "a", "ourdomain", ["Type: Studio"])
        _add(temp_db, "b", "magis", ["Type: studio"])
        assert temp_db.get_feature_values("Type") == ["Studio"]

    def test_keeps_the_capitalized_spelling(self, temp_db):
        """注意：这条**不能**证明与行序无关。

        ``get_feature_values`` 的 SQL 带 ``ORDER BY val``，二进制排序下大写
        字母永远排在小写前，所以哪怕把选择规则整个换成"先到先得"，走数据库
        这条路照样得到 "Studio"。行序无关性由
        ``TestDedupeIsOrderIndependent`` 直接喂序列来证明。
        """
        _add(temp_db, "a", "magis", ["Type: studio"])
        _add(temp_db, "b", "ourdomain", ["Type: Studio"])
        assert temp_db.get_feature_values("Type") == ["Studio"]

    def test_does_not_merge_genuinely_different_values(self, temp_db):
        _add(temp_db, "a", "holland2stay", ["Type: Studio"])
        _add(temp_db, "b", "plaza", ["Type: House"])
        _add(temp_db, "c", "magis", ["Type: 2-room apartment"])
        assert sorted(temp_db.get_feature_values("Type")) == [
            "2-room apartment", "House", "Studio",
        ]

    def test_all_lowercase_variants_pick_a_stable_one(self, temp_db):
        """一个大写的都没有时也必须收敛到同一个，不能看行序。"""
        _add(temp_db, "a", "magis", ["Type: studio"])
        _add(temp_db, "b", "xior", ["Type: sTuDiO"])
        got = temp_db.get_feature_values("Type")
        assert len(got) == 1, got

    def test_synonym_merging_still_works(self, temp_db):
        """大小写去重不能把原有的同义词归一顶掉（荷/英两版房型）。"""
        _add(temp_db, "a", "holland2stay", ["Type: Loft (open bedroom area)"])
        _add(temp_db, "b", "holland2stay", ["Type: Loft (open slaapkamer)"])
        assert temp_db.get_feature_values("Type") == ["Loft (open bedroom area)"]

    def test_other_dimensions_are_deduped_too(self, temp_db):
        _add(temp_db, "a", "holland2stay", ["Tenant: Students only"])
        _add(temp_db, "b", "magis", ["Tenant: students only"])
        assert temp_db.get_feature_values("Tenant") == ["Students only"]


class TestPreferSpelling:
    """判据必须与出现条数无关，且是**全序**——否则结果取决于行序。"""

    def test_capitalized_wins(self):
        assert _prefer_spelling("Studio", "studio") is True
        assert _prefer_spelling("studio", "Studio") is False

    def test_same_case_falls_back_to_sort_order(self):
        assert _prefer_spelling("Apple", "Banana") is True
        assert _prefer_spelling("Banana", "Apple") is False

    def test_is_antisymmetric(self):
        """a 优于 b 与 b 优于 a 不能同时成立，否则结果依赖行序。"""
        variants = ["Studio", "studio", "STUDIO", "sTuDiO"]
        for a in variants:
            for b in variants:
                if a == b:
                    continue
                assert not (_prefer_spelling(a, b) and _prefer_spelling(b, a)), (a, b)

    def test_reduction_order_does_not_matter(self):
        """任意顺序折叠同一组写法，结果都一样。"""
        import itertools
        for perm in itertools.permutations(["Studio", "studio", "STUDIO"]):
            kept = perm[0]
            for v in perm[1:]:
                if _prefer_spelling(v, kept):
                    kept = v
            assert kept == "STUDIO", perm


class TestDedupeIsOrderIndependent:
    """直接喂序列——这里才真正测到"保留哪个写法"这条规则。

    走数据库测不到：``ORDER BY val`` 已经把大写排在前面，"先到先得"和
    "首字母大写优先"给出同一个答案，删掉规则测试照样全绿。
    """

    def test_lowercase_first_still_yields_the_capitalized_spelling(self):
        assert dedupe_feature_values(["studio", "Studio"]) == ["Studio"]

    def test_every_permutation_gives_the_same_result(self):
        import itertools
        variants = ["studio", "Studio", "STUDIO"]
        results = {tuple(dedupe_feature_values(list(p)))
                   for p in itertools.permutations(variants)}
        assert results == {("STUDIO",)}, results

    def test_order_of_distinct_values_follows_first_appearance(self):
        """不同的值不该被重排——调用方（SQL）已经排好序了。"""
        assert dedupe_feature_values(["Studio", "House", "Apartment"]) == [
            "Studio", "House", "Apartment",
        ]

    def test_blank_and_none_are_dropped(self):
        assert dedupe_feature_values(["", "Studio", None]) == ["Studio"]

    def test_synonyms_merge_across_languages(self):
        got = dedupe_feature_values(
            ["Loft (open slaapkamer)", "Loft (open bedroom area)"])
        assert got == ["Loft (open bedroom area)"]
