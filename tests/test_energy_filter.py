"""
Energy / Furnishing 过滤逻辑测试。

覆盖：
- energy_rank() 正常/边界/非法输入
- ListingFilter.allowed_energy 过滤语义（最低可接受等级）
- ListingFilter.allowed_finishing 白名单过滤
- users._lf_from_dict() 新旧字段 round-trip + 旧 list 兼容
- /listings?energy=... API 端点
"""
from __future__ import annotations

import pytest

from config import energy_rank, ListingFilter
from models import Listing


# ── energy_rank ──────────────────────────────────────────

class TestEnergyRank:
    def test_a_triple_plus(self):
        assert energy_rank("A+++") == 0

    def test_a_double_plus(self):
        assert energy_rank("A++") == 1

    def test_a_single_plus(self):
        assert energy_rank("A+") == 2

    def test_a_plain(self):
        assert energy_rank("A") == 3

    def test_b(self):
        assert energy_rank("B") == 4

    def test_c(self):
        assert energy_rank("C") == 5

    def test_d(self):
        assert energy_rank("D") == 6

    def test_e(self):
        assert energy_rank("E") == 7

    def test_lowercase_normalized(self):
        assert energy_rank("a") == 3
        assert energy_rank("b") == 4

    def test_whitespace_stripped(self):
        assert energy_rank("  A  ") == 3

    def test_empty_string(self):
        assert energy_rank("") is None

    def test_non_string_input(self):
        assert energy_rank(None) is None  # type: ignore[arg-type]
        assert energy_rank(123) is None   # type: ignore[arg-type]

    def test_only_plus_signs(self):
        assert energy_rank("+++") is None

    def test_unrecognized_label(self):
        assert energy_rank("G") is None
        assert energy_rank("XYZ") is None

    def test_partial_match_rejected(self):
        """白名单精确匹配，'banana' 不应被当成 B。"""
        assert energy_rank("banana") is None
        assert energy_rank("AA") is None
        assert energy_rank("B++") is None  # 仅 A 有 + 等级
        assert energy_rank("Z") is None

    def test_ordering_consistency(self):
        """所有已知等级排序应正确：A+++ < A++ < A+ < A < B < C < D"""
        labels = ["B", "A++", "A", "D", "A+++", "C", "A+"]
        sorted_labels = sorted(
            labels,
            key=lambda x: energy_rank(x) if energy_rank(x) is not None else 99,
        )
        assert sorted_labels == ["A+++", "A++", "A+", "A", "B", "C", "D"]


# ── ListingFilter.allowed_energy ──────────────────────────

def _listing(**overrides):
    """最小 Listing，覆盖 feature_map 用 features 列表构造。"""
    defaults = {
        "id": "test-1",
        "name": "Test",
        "status": "Available to book",
        "price_raw": "€1000",
        "available_from": "2026-06-01",
        "features": [
            "Type: Studio",
            "Area: 30.0 m²",
            "Energy: A",
            "Finishing: Upholstered",
        ],
        "url": "https://example.com",
        "city": "Eindhoven",
        "sku": "SKU1",
        "contract_id": 1,
        "contract_start_date": None,
    }
    defaults.update(overrides)
    return Listing(**defaults)


class TestListingFilterEnergy:
    def test_min_b_passes_a(self):
        """最低 B → A 应通过（A 优于 B）。"""
        lf = ListingFilter(allowed_energy="B")
        assert lf.passes(_listing(features=["Energy: A"]))

    def test_min_b_passes_b(self):
        """最低 B → B 应通过（平级）。"""
        lf = ListingFilter(allowed_energy="B")
        assert lf.passes(_listing(features=["Energy: B"]))

    def test_min_b_rejects_c(self):
        """最低 B → C 应拒绝（C 差于 B）。"""
        lf = ListingFilter(allowed_energy="B")
        assert not lf.passes(_listing(features=["Energy: C"]))

    def test_min_a_plus_passes_a_plus_plus(self):
        """最低 A+ → A++ 应通过。"""
        lf = ListingFilter(allowed_energy="A+")
        assert lf.passes(_listing(features=["Energy: A++"]))

    def test_min_a_rejects_b(self):
        lf = ListingFilter(allowed_energy="A")
        assert not lf.passes(_listing(features=["Energy: B"]))

    def test_missing_energy_label_rejected(self):
        """设置了最低要求但房源无能耗标签 → 拒绝（fail-closed）。"""
        lf = ListingFilter(allowed_energy="B")
        assert not lf.passes(_listing(features=[]))

    def test_empty_energy_means_no_filter(self):
        """不设 allowed_energy → 全部通过。"""
        lf = ListingFilter(allowed_energy="")
        assert lf.passes(_listing(features=["Energy: A"]))
        assert lf.passes(_listing(features=["Energy: C"]))
        assert lf.passes(_listing(features=[]))

    def test_white_space_only_means_no_filter(self):
        lf = ListingFilter(allowed_energy="   ")
        assert lf.passes(_listing(features=["Energy: A"]))

    def test_old_list_value_treated_as_empty(self):
        """旧 users.json 存 ['A', 'B'] → 当作不设过滤。"""
        lf = ListingFilter(allowed_energy=["A", "B"])  # type: ignore[arg-type]
        assert lf.passes(_listing(features=["Energy: A"]))
        assert lf.passes(_listing(features=["Energy: C"]))

    def test_is_empty_with_list_value(self):
        lf = ListingFilter(allowed_energy=["A"])  # type: ignore[arg-type]
        assert lf.is_empty()


# ── ListingFilter.allowed_finishing ───────────────────────

    def test_invalid_config_fail_closed(self):
        """配置了非法等级（如 'banana'）→ fail-closed，拒绝所有房源。"""
        lf = ListingFilter(allowed_energy="banana")
        assert not lf.passes(_listing(features=["Energy: A"]))

    def test_listing_has_unknown_label_rejected(self):
        """房源标签不在白名单中（如 'Z'），设置了最低要求 → 拒绝。"""
        lf = ListingFilter(allowed_energy="B")
        assert not lf.passes(_listing(features=["Energy: Z"]))


class TestListingFilterFurnishing:
    def test_upholstered_passes(self):
        lf = ListingFilter(allowed_finishing=["Upholstered"])
        assert lf.passes(_listing(features=["Finishing: Upholstered"]))

    def test_shell_rejected(self):
        lf = ListingFilter(allowed_finishing=["Upholstered"])
        assert not lf.passes(_listing(features=["Finishing: Shell"]))

    def test_case_insensitive(self):
        lf = ListingFilter(allowed_finishing=["upholstered"])
        assert lf.passes(_listing(features=["Finishing: Upholstered"]))

    def test_substring_match(self):
        lf = ListingFilter(allowed_finishing=["Upholstered"])
        assert lf.passes(_listing(features=["Finishing: Upholstered (basic)"]))

    def test_missing_feature_rejected(self):
        lf = ListingFilter(allowed_finishing=["Upholstered"])
        assert not lf.passes(_listing(features=[]))

    def test_empty_list_means_no_filter(self):
        lf = ListingFilter(allowed_finishing=[])
        assert lf.passes(_listing(features=["Finishing: Shell"]))


# ── _lf_from_dict round-trip + 旧兼容 ─────────────────────

class TestLfFromDictRoundTrip:
    def test_energy_str_round_trip(self):
        from users import _lf_from_dict
        d = {"allowed_energy": "B", "allowed_finishing": ["Upholstered"]}
        lf = _lf_from_dict(d)
        assert lf.allowed_energy == "B"
        assert lf.allowed_finishing == ["Upholstered"]

    def test_energy_old_list_normalized_to_str(self):
        """旧 users.json 格式: allowed_energy 存为 list → 归一化为 ''。"""
        from users import _lf_from_dict
        d = {"allowed_energy": ["A", "B"]}
        lf = _lf_from_dict(d)
        assert lf.allowed_energy == ""

    def test_energy_missing_defaults_to_empty(self):
        from users import _lf_from_dict
        lf = _lf_from_dict({})
        assert lf.allowed_energy == ""
        assert lf.allowed_finishing == []

    def test_energy_old_int_normalized(self):
        """极端：旧数据存了非预期类型 → 归一化。"""
        from users import _lf_from_dict
        d = {"allowed_energy": 42}
        lf = _lf_from_dict(d)
        assert lf.allowed_energy == ""


# ── /listings?energy=... API ─────────────────────────────

class TestListingsEnergyFilter:
    def test_energy_param_passed(self, admin_client):
        """验证 energy filter 至少不 500。"""
        r = admin_client.get("/listings?energy=B")
        assert r.status_code == 200

    def test_energy_and_finishing_combined(self, admin_client):
        r = admin_client.get("/listings?energy=A&finishing=Upholstered")
        assert r.status_code == 200

    def test_invalid_energy_no_crash(self, admin_client):
        """非法能耗值不抛 500。"""
        for e in ["INVALIDGRADE", "Z", "banana", "AA"]:
            r = admin_client.get(f"/listings?energy={e}")
            assert r.status_code == 200, f"?energy={e} returned {r.status_code}"

    def test_energy_empty_select_shows_all(self, admin_client):
        r = admin_client.get("/listings?energy=")
        assert r.status_code == 200
