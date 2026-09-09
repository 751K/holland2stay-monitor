"""``area_value`` / ``energy_rank``：从 features 派生的排序列。

为什么要落成列
--------------
两个值在 ``features`` 里是**文本**。按文本排是字典序：``"9 m²"`` 排在
``"87.28 m²"`` 后面，``"A+++"`` 排在 ``"A"`` 前面（``+`` 的码位比空格大）。

这个文件钉三件事：算得对、**每个写 features 的地方都跟着算**、迁移能把存量补上。
中间那条是最容易漏的——2026-09-05 的 ``os_version`` 就是只写了 INSERT 分支，
表现是「新数据没问题，存量永远是空」，而所有测试都绿。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from models import Listing
from mstorage._derived import derived_from_features


def _feats(area=None, energy=None, extra=()):
    out = list(extra)
    if area is not None:
        out.append(f"Area: {area} m²")
    if energy is not None:
        out.append(f"Energy: {energy}")
    return out


def _listing(lid, *, area=None, energy=None, status="Available to book"):
    return Listing(id=lid, name=f"L {lid}", status=status, price_raw="€1000",
                   available_from="2030-01-01", features=_feats(area, energy),
                   url=f"http://x/{lid}", city="Eindhoven", source="holland2stay")


class TestDeriving:

    @pytest.mark.parametrize("area,energy,want", [
        ("66", "A", (66.0, 3)),
        ("87.28", "B", (87.28, 4)),
        ("9", "A+++", (9.0, 0)),
        (None, "F", (None, 8)),
        ("50", None, (50.0, None)),
        (None, None, (None, None)),
    ])
    def test_values(self, area, energy, want):
        assert derived_from_features(json.dumps(_feats(area, energy))) == want

    def test_the_feature_key_is_energy_label_not_energy(self):
        """``LISTING_KEY_MAP`` 把 ``"Energy"`` 映成 ``energy_label``。

        写成 ``energy`` 的话每一条都算不出等级，而且**不会报错**——列全是 NULL，
        排序照常"能用"，只是所有房子并列排在最后。开发时就踩过这一脚，靠拿真数据
        试才发现。
        """
        from models import LISTING_KEY_MAP
        assert LISTING_KEY_MAP["Energy"] == "energy_label"
        # "A" 是 ENERGY_LABELS 的第 4 项（A+++ / A++ / A+ / A），所以 rank 是 3。
        # 关键是它**不为 None**——写错 key 的表现就是永远 None。
        assert derived_from_features(json.dumps(["Energy: A"]))[1] == 3

    @pytest.mark.parametrize("bad", [None, "", "不是 JSON", "{}", '"字符串"', "[1,2]"])
    def test_garbage_never_raises(self, bad):
        """features 是自由文本，坏一条不该让整轮入库炸掉。"""
        assert derived_from_features(bad) == (None, None)

    def test_zero_area_is_unknown_not_zero(self):
        """0 平米不是房子，是脏数据。当 0 存的话它会排在"最小"那一端。

        负数进不来——``parse_float("-5 m²")`` 给的是 ``5.0``，它按数字子串抽，
        减号被当成分隔符丢掉了。所以这里只测 0。
        """
        assert derived_from_features(json.dumps(_feats("0", "A")))[0] is None
        from models import parse_float
        assert parse_float("-5 m²") == 5.0, "parse_float 行为变了，上面那句注释要改"

    def test_unknown_energy_label_is_none(self):
        assert derived_from_features(json.dumps(["Energy: Z"]))[1] is None

    def test_energy_rank_is_best_first(self):
        ranks = [derived_from_features(json.dumps([f"Energy: {l}"]))[1]
                 for l in ("A+++", "A", "B", "F")]
        assert ranks == sorted(ranks), "越小越好这条反了"
        assert ranks[0] == 0


class TestEveryWritePathKeepsThemInSync:
    """写 features 的地方都要跟着写派生列。"""

    def test_insert(self, temp_db):
        temp_db.diff([_listing("n1", area="66", energy="A")])
        assert temp_db._conn.execute(
            "SELECT area_value, energy_rank FROM listings WHERE id='n1'"
        ).fetchone()[:2] == (66.0, 3)

    def test_update_refreshes_them(self, temp_db):
        """**这条是重点。** 只写 INSERT 的话，存量房源的面积永远停在第一次入库时
        的值——而 features 是会变的（上游改了面积、补了能耗标签）。
        """
        temp_db.diff([_listing("u1", area="50", energy="D")])
        temp_db.diff([_listing("u1", area="88", energy="A")])
        assert temp_db._conn.execute(
            "SELECT area_value, energy_rank FROM listings WHERE id='u1'"
        ).fetchone()[:2] == (88.0, 3)

    def test_losing_a_feature_clears_the_column(self, temp_db):
        """上游不再报面积时，列要跟着变回 NULL，而不是留着旧值。"""
        temp_db.diff([_listing("c1", area="50", energy="D")])
        temp_db.diff([_listing("c1")])
        assert temp_db._conn.execute(
            "SELECT area_value, energy_rank FROM listings WHERE id='c1'"
        ).fetchone()[:2] == (None, None)

    def test_every_sql_that_writes_features_also_writes_them(self):
        """静态兜底：将来再加一处写 features 的 SQL，也得带上派生列。

        这两个值和 features 是同一个事实的两种形态。哪天有人加了第六处写入点却
        忘了派生列，表现是排序安静地按旧数据来——没有报错，没有日志。
        """
        pat = re.compile(r"features\s*=\s*\?")
        offenders = []
        for path in sorted(Path("mstorage").glob("*.py")):
            src = path.read_text(encoding="utf-8")
            for m in pat.finditer(src):
                stmt = src[max(0, m.start() - 400):m.start() + 400]
                if "area_value" in stmt or "_backfill_derived_sort_columns" in stmt:
                    continue
                line = src[:m.start()].count("\n") + 1
                offenders.append(f"{path}:{line}")
        assert not offenders, (
            "这些地方写了 features 却没同时更新 area_value / energy_rank：\n  "
            + "\n  ".join(offenders))


class TestMigration:

    def test_backfill_fills_existing_rows(self, temp_db):
        """老库加列之后，存量要被补上——否则排序对历史数据全是"未知"。"""
        temp_db.diff([_listing("m1", area="66", energy="A"),
                      _listing("m2", area="87.28", energy="B")])
        with temp_db._conn:
            temp_db._conn.execute(
                "UPDATE listings SET area_value=NULL, energy_rank=NULL")

        assert temp_db._backfill_derived_sort_columns() == 2
        got = dict(
            (r[0], (r[1], r[2])) for r in temp_db._conn.execute(
                "SELECT id, area_value, energy_rank FROM listings"))
        assert got == {"m1": (66.0, 3), "m2": (87.28, 4)}

    def test_backfill_can_target_specific_ids(self, temp_db):
        temp_db.diff([_listing("a1", area="10"), _listing("a2", area="20")])
        with temp_db._conn:
            temp_db._conn.execute("UPDATE listings SET area_value=NULL")
        assert temp_db._backfill_derived_sort_columns(["a1"]) == 1
        got = dict(temp_db._conn.execute("SELECT id, area_value FROM listings"))
        assert got == {"a1": 10.0, "a2": None}

    def test_empty_id_list_is_a_noop(self):
        from mstorage._base import StorageBase
        assert StorageBase._backfill_derived_sort_columns(object(), []) == 0

    def test_columns_are_nullable_with_no_default(self, temp_db):
        """NULL 必须能表示「不知道」。给默认 0 的话，没面积的房子会挤在最优端。"""
        cols = {r[1]: r for r in temp_db._conn.execute("PRAGMA table_info(listings)")}
        for name in ("area_value", "energy_rank"):
            assert name in cols, f"{name} 不在表里"
            assert cols[name][3] == 0, f"{name} 被标成 NOT NULL"
            assert cols[name][4] is None, f"{name} 有默认值 {cols[name][4]!r}"
