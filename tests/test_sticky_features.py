"""详情补齐出来的 feature 是**粘性**的：抓取侧没给 ≠ 上游没有了。

背景：Building / Tenant / Neighborhood / MinIncome 不是列表抓取产出的，而是另发
一次 GetProductDetail 补齐的（白名单主查询的字段集里没有它们）。补齐按预算 + 限速
跨轮渐进，会因 429 中断，进程重启后缓存还清零。

而 diff() 每轮整体覆盖 features。两者一叠加：同一条房源在「补到了」和「还没补到」
之间来回，没补到的那轮就把上一轮存好的值**抹掉**。实测：每次部署后仪表盘的「楼盘」
列大面积变回 '—'，要 90 分钟才慢慢长回来。
"""
from __future__ import annotations

import json

import pytest

from mstorage._listings import _STICKY_FEATURE_KEYS, _merge_sticky_features
from models import Listing


class TestMergeHelper:
    def test_missing_sticky_key_is_restored(self):
        got = _merge_sticky_features(
            ["Type: Studio"], json.dumps(["Building: The Wall", "Type: Loft"]))
        assert "Building: The Wall" in got

    def test_fresh_value_wins_when_present(self):
        """上游真改了楼盘名要能跟上——粘性不等于冻结。"""
        got = _merge_sticky_features(
            ["Building: New"], json.dumps(["Building: Old"]))
        assert got == ["Building: New"]

    def test_non_sticky_keys_are_not_restored(self):
        """普通字段每轮都拿得到，缺失就是真的没有了，不该粘。"""
        got = _merge_sticky_features(
            ["Building: X"], json.dumps(["Type: Loft", "Area: 40 m²"]))
        assert got == ["Building: X"]

    @pytest.mark.parametrize("bad", [None, "", "not json", "{}", "[123]"])
    def test_corrupt_old_features_are_ignored(self, bad):
        """旧值坏了就当没有，绝不能因此抛异常——这是写库路径。"""
        assert _merge_sticky_features(["Type: Studio"], bad) == ["Type: Studio"]

    def test_all_enrichment_keys_are_covered(self):
        """粘性表必须覆盖详情补齐产出的全部 key，漏一个那个字段就会闪。"""
        import scrapers.holland2stay as h2s
        produced = set(h2s._detail_features(
            {"building_name": 614, "tenant_profile": 6213,
             "neighborhood": "Strijp", "min_income": "3.5"},
            [{"attribute_code": "building_name",
              "options": [{"value": "614", "label": "X"}]}],
        ))
        assert produced <= set(_STICKY_FEATURE_KEYS), (
            f"详情补齐产出了 {produced - set(_STICKY_FEATURE_KEYS)}，"
            f"但它不在粘性表里——会在补齐失败的轮次被抹掉"
        )


def _feats(row) -> list[str]:
    """get_all_listings 返回 dict，features 存的是 JSON 字符串。"""
    raw = row["features"] if isinstance(row, dict) else row.features
    return json.loads(raw) if isinstance(raw, str) else list(raw)


class TestDiffKeepsStickyFeatures:
    """真正走一遍 storage.diff()，确认落库行为。"""

    @staticmethod
    def _l(features, status="Occupied"):
        return Listing(
            id="L1", name="L1", status=status, price_raw="€1000",
            available_from="", url="", city="Eindhoven",
            source="holland2stay", features=list(features),
        )

    def test_building_survives_a_round_without_enrichment(self, tmp_path):
        """核心回归：补齐过一次之后，没补到的那轮不能把它抹掉。"""
        from storage import Storage
        st = Storage(tmp_path / "t.db", timezone_str="UTC")
        try:
            st.diff([self._l(["Type: Studio", "Building: The Wall"])])
            # 下一轮补齐没跑到这条（429 / 预算 / 重启），features 里没有 Building
            st.diff([self._l(["Type: Studio"])])
            row = st.get_all_listings()[0]
            assert "Building: The Wall" in _feats(row), (
                "楼盘被抹了——部署后仪表盘会大面积变回 '—'"
            )
        finally:
            st.close()

    def test_status_change_round_also_keeps_it(self, tmp_path):
        """状态变更走的是另一条 UPDATE 分支，同样不能抹。"""
        from storage import Storage
        st = Storage(tmp_path / "t.db", timezone_str="UTC")
        try:
            st.diff([self._l(["Building: The Wall"], status="Occupied")])
            st.diff([self._l(["Type: Studio"], status="Available to book")])
            row = st.get_all_listings()[0]
            assert row["status"] == "Available to book"
            assert "Building: The Wall" in _feats(row)
        finally:
            st.close()

    def test_upstream_rename_still_propagates(self, tmp_path):
        """反向守卫：粘性不能变成冻结，上游改名要能覆盖。"""
        from storage import Storage
        st = Storage(tmp_path / "t.db", timezone_str="UTC")
        try:
            st.diff([self._l(["Building: Old Name"])])
            st.diff([self._l(["Building: New Name"])])
            row = st.get_all_listings()[0]
            assert "Building: New Name" in _feats(row)
            assert "Building: Old Name" not in _feats(row)
        finally:
            st.close()
