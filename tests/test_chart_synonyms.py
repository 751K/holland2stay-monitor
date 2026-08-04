"""图表取值的同义合并。

2026-08-04 生产实测：**同一个平台**（holland2stay）对同一种合同既返回
``Indefinite``（178 条）又返回 ``Onbepaalde tijd``（38 条），于是「合同类型
分布」把一件事画成了两块，还有一块是荷兰语。

翻译解决不了这个——翻译只会让两块都变成中文，该合的还是没合。必须在计数
阶段合并。

放在读取层而不是抓取层：抓取层只能修以后的数据，库里已有的 38 条还是错的；
读取层一改，历史数据同时正确。
"""
from __future__ import annotations

from mstorage._charts import _FEATURE_SYNONYMS, _merge_synonyms


class TestMerging:
    def test_dutch_and_english_indefinite_are_one_bucket(self):
        got = _merge_synonyms([
            {"label": "Indefinite", "count": 178},
            {"label": "Onbepaalde tijd", "count": 38},
            {"label": "6 months max", "count": 72},
        ])
        assert {r["label"]: r["count"] for r in got} == {
            "Indefinite": 216, "6 months max": 72,
        }

    def test_result_stays_sorted_by_count(self):
        """合并会改变名次——178 排第一，合并后 216 仍该第一。"""
        got = _merge_synonyms([
            {"label": "6 months max", "count": 100},
            {"label": "Indefinite", "count": 80},
            {"label": "Onbepaalde tijd", "count": 40},
        ])
        assert [r["label"] for r in got] == ["Indefinite", "6 months max"]
        assert got[0]["count"] == 120

    def test_matching_ignores_case_and_padding(self):
        got = _merge_synonyms([
            {"label": "Indefinite", "count": 1},
            {"label": "  ONBEPAALDE TIJD  ", "count": 2},
        ])
        assert got == [{"label": "Indefinite", "count": 3}]

    def test_unknown_values_pass_through_untouched(self):
        """新出现的取值不该被吞掉，也不该被塞进某个已知桶。"""
        got = _merge_synonyms([{"label": "Iets nieuws", "count": 5}])
        assert got == [{"label": "Iets nieuws", "count": 5}]

    def test_empty_input(self):
        assert _merge_synonyms([]) == []


class TestSynonymTable:
    def test_keys_are_casefolded(self):
        """查表用 casefold，键没规范化就永远命中不了。"""
        for k in _FEATURE_SYNONYMS:
            assert k == k.casefold(), f"{k!r} 应当是 casefold 之后的形式"

    def test_no_cycles(self):
        """规范值本身不能又是别的同义词的键，否则合并结果取决于顺序。"""
        for canonical in _FEATURE_SYNONYMS.values():
            assert canonical.casefold() not in _FEATURE_SYNONYMS
