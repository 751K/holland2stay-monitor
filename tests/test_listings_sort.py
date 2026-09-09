"""``GET /listings?sort=`` —— 排序、稳定兜底、未知值沉底、非法值 400。

四件事里 **``id`` 兜底比排序本身更要紧**：加 sort 之前，顺序来自
``get_all_listings`` 里那句 ``ORDER BY first_seen DESC``，规格里从没承诺过它，
而三端的 ``offset`` 分页全建在这个没承诺的顺序上。有并列值时，两次请求之间的
相对顺序可以不同——翻页会重复一条、漏掉另一条，而且不报任何错。
"""
from __future__ import annotations

import pytest

from app.services.listing_service import (
    DEFAULT_SORT,
    SORT_KEYS,
    SortError,
    parse_sort,
    sort_listing_rows,
    status_rank,
)


def _row(rid, *, price="€1000", first_seen="2026-01-01", last_seen="2026-01-01",
         available_from="2026-06-01", city="Eindhoven", status="Available to book",
         source="holland2stay"):
    return {"id": rid, "price_raw": price, "first_seen": first_seen,
            "last_seen": last_seen, "available_from": available_from,
            "city": city, "status": status, "source": source}


def _ids(rows, key, desc=False):
    return [r["id"] for r in sort_listing_rows(rows, key, desc)]


class TestParsing:

    def test_default_is_part_of_the_contract(self):
        assert DEFAULT_SORT == "-first_seen"
        assert parse_sort(None) == ("first_seen", True)
        assert parse_sort("") == ("first_seen", True)

    @pytest.mark.parametrize("key", SORT_KEYS)
    def test_every_documented_key_parses(self, key):
        assert parse_sort(key) == (key, False)
        assert parse_sort("-" + key) == (key, True)

    @pytest.mark.parametrize("bad", ["pirce", "-pirce", "areaa", "enrgy",
                                     "name", "id", "--price", "price-"])
    def test_unknown_keys_raise(self, bad):
        """**不静默回退。** 回退的话返回的是一份错的顺序，而客户端无从察觉。"""
        with pytest.raises(SortError):
            parse_sort(bad)

    def test_multi_key_is_rejected_not_truncated(self):
        """逗号形式留给将来；现在给了就报错，而不是悄悄只用第一个键。"""
        with pytest.raises(SortError):
            parse_sort("city,-price")

    def test_area_and_energy_are_in(self):
        """它们最初不在 v1 里——features 里存的是文本，按文本排是字典序。

        2026-09-09 落成派生列 area_value / energy_rank 之后进来。派生列本身的
        测试在 tests/test_derived_sort_columns.py。
        """
        assert "area" in SORT_KEYS and "energy" in SORT_KEYS


class TestStableTiebreak:
    """并列时按 id，升降序都是。"""

    def test_ties_break_on_id_ascending(self):
        rows = [_row("c"), _row("a"), _row("b")]      # 三条完全并列
        assert _ids(rows, "price") == ["a", "b", "c"]
        assert _ids(rows, "price", desc=True) == ["a", "b", "c"]

    def test_order_does_not_depend_on_input_order(self):
        """**输入顺序变了，输出顺序也必须一样。**

        这才是兜底键真正买到的东西。上一版写的是「翻一遍页，每条恰好出现一次」
        ——那条在没有兜底键时**照样是绿的**：Python 的 sort 稳定，全并列的输入
        原样输出，当然不重不漏。它测的是「切片会不会丢东西」，而风险根本不在
        切片，在**两次请求拿到的底层行序可能不同**。

        分页正是这么坏掉的：第 1 页按一种顺序切，第 2 页按另一种顺序切，于是
        同一条出现两次、另一条一次也没出现，而且不报任何错。
        """
        import random

        rows = [_row(f"id-{i:02d}") for i in range(25)]     # 25 条完全并列
        baseline = [r["id"] for r in sort_listing_rows(rows, "price", False)]
        for seed in range(8):
            shuffled = rows[:]
            random.Random(seed).shuffle(shuffled)
            assert [r["id"] for r in sort_listing_rows(shuffled, "price", False)] \
                == baseline, "底层行序一变，输出顺序就变——分页会重复或漏项"

    def test_paging_covers_every_row_exactly_once(self):
        rows = [_row(f"id-{i:02d}") for i in range(25)]
        ordered = sort_listing_rows(rows, "price", False)
        seen = [r["id"] for i in range(0, 25, 10) for r in ordered[i:i + 10]]
        assert sorted(seen) == sorted(r["id"] for r in rows)


class TestUnknownValuesSinkToTheBottom:
    """升降序都排最后。"""

    def test_unparseable_price(self):
        rows = [_row("a", price="€900"), _row("b", price=""),
                _row("c", price="€1200")]
        assert _ids(rows, "price") == ["a", "c", "b"]
        assert _ids(rows, "price", desc=True) == ["c", "a", "b"]

    def test_empty_available_from(self):
        rows = [_row("a", available_from="2026-06-01"),
                _row("b", available_from=""),
                _row("c", available_from="2026-03-01")]
        assert _ids(rows, "available_from") == ["c", "a", "b"]

    def test_sentinel_available_from_counts_as_unknown(self):
        """2050 哨兵不是日期，是「不知道」。

        当日期排的话，「最早可入住」的第一屏全是它——比空值更糟，因为它看起来
        像个真日期。
        """
        rows = [_row("a", available_from="2050-01-01"),
                _row("b", available_from="2026-12-01"),
                _row("c", available_from="2099-01-01")]
        assert _ids(rows, "available_from") == ["b", "a", "c"]
        assert _ids(rows, "available_from", desc=True) == ["b", "a", "c"]


class TestStatusOrder:

    @pytest.mark.parametrize("status,rank", [
        ("Available to book", 0), ("available_to_book", 0), ("book", 0),
        ("Available in lottery", 1), ("available_in_lottery", 1),
        ("Reserved", 2), ("reserved", 2),
        ("Occupied", 3), ("Rented", 3), ("Not available", 3),
        ("not_available", 3),
        ("", 9), ("something new", 9),
    ])
    def test_rank(self, status, rank):
        assert status_rank(status) == rank

    def test_not_lexical(self):
        """字典序会把 lottery 排在 book 前面——那毫无意义。"""
        rows = [_row("occ", status="Occupied"),
                _row("lot", status="Available in lottery"),
                _row("bok", status="Available to book"),
                _row("res", status="Reserved")]
        assert _ids(rows, "status") == ["bok", "lot", "res", "occ"]
        # 反例：真按字典序排会是 lottery < book < Occupied < Reserved
        assert _ids(rows, "status") != sorted(
            (r["id"] for r in rows), key=lambda i: {
                "lot": "Available in lottery", "bok": "Available to book",
                "occ": "Occupied", "res": "Reserved"}[i])


class TestHttp:

    def test_default_order_is_newest_first(self, admin_client):
        r = admin_client.get("/api/v1/listings")
        assert r.status_code == 200

    def test_unknown_sort_is_400_with_the_allowed_values(self, admin_client):
        r = admin_client.get("/api/v1/listings?sort=pirce")
        assert r.status_code == 400, "静默回退了——客户端看不出顺序是错的"
        body = r.get_json()
        assert body["ok"] is False
        msg = body["error"]["message"]
        assert "pirce" in msg
        for key in ("price", "first_seen", "status"):
            assert key in msg, f"400 里没给出可用值 {key}"

    @pytest.mark.parametrize("key", SORT_KEYS)
    def test_every_documented_key_is_accepted(self, admin_client, key):
        assert admin_client.get(f"/api/v1/listings?sort={key}").status_code == 200
        assert admin_client.get(f"/api/v1/listings?sort=-{key}").status_code == 200

    def test_multi_key_is_400(self, admin_client):
        assert admin_client.get(
            "/api/v1/listings?sort=city,-price").status_code == 400


class TestAreaAndEnergy:
    """走派生列，不是每次现从 features 里抽。"""

    def _row(self, rid, area=None, rank=None):
        return {"id": rid, "area_value": area, "energy_rank": rank}

    def test_area_is_numeric_not_lexical(self):
        """按文本排的话 "9 m²" 会排到 "87.28 m²" 后面。"""
        rows = [self._row("big", 87.28), self._row("small", 9.0)]
        assert _ids(rows, "area") == ["small", "big"]
        assert _ids(rows, "area", desc=True) == ["big", "small"]

    def test_energy_ascending_is_best_first(self):
        """rank 越小越好（A+++ = 0），所以升序 = 最好的在前。"""
        rows = [self._row("f", rank=8), self._row("aaa", rank=0),
                self._row("b", rank=4)]
        assert _ids(rows, "energy") == ["aaa", "b", "f"]

    def test_missing_values_sink_in_both_directions(self):
        rows = [self._row("has", 66.0, 3), self._row("none")]
        assert _ids(rows, "area") == ["has", "none"]
        assert _ids(rows, "area", desc=True) == ["has", "none"]
        assert _ids(rows, "energy") == ["has", "none"]
        assert _ids(rows, "energy", desc=True) == ["has", "none"]
