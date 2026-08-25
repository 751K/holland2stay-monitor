"""同一张表的同一组列上不许挂两个索引。

起因（2026-08-25）
------------------
生产库上量出 ``status_changes`` 有两对完全重复的索引::

    idx_sc_changed_at   ≡  idx_status_changes_changed_at    (changed_at)
    idx_sc_listing_id   ≡  idx_status_changes_listing_id    (listing_id)

成因是 v1.3.0（2026-05-13，storage 模块化重构）里的一次**改名**：旧名从代码里
删掉、新名用 ``CREATE INDEX IF NOT EXISTS`` 建上，但没人 DROP 旧的。而
``IF NOT EXISTS`` 只看名字，同定义的另一个名字它管不着——于是存量库带着这两对
跑了三个半月，每 INSERT 一条 status_changes 要维护 4 个索引而不是 2 个。

本文件守的不是「这两个名字没了」，而是**这类事不再发生**：按 (表, 列组) 归并，
任何一组挂两个索引都算重复。下次再改名忘了 DROP，这里会红。

只比列，不比名字——名字正是上次骗过所有人的东西。
"""
from __future__ import annotations

from collections import defaultdict

import pytest

#: 这两个是本次要清掉的旧名。它们已经不在建表代码里，只可能来自存量库。
STALE_INDEX_NAMES = ("idx_sc_changed_at", "idx_sc_listing_id")


def _index_signatures(conn) -> dict[tuple, list[str]]:
    """{(表, (列, ...)): [索引名, ...]}，跳过 SQLite 自建的唯一索引。"""
    sigs: dict[tuple, list[str]] = defaultdict(list)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'"
    )]
    for t in tables:
        for idx in conn.execute(f"PRAGMA index_list({t})"):
            name, origin = idx[1], idx[3]
            if origin != "c":       # 'c' = CREATE INDEX；'u'/'pk' 是约束自动建的
                continue
            cols = tuple(r[2] for r in conn.execute(f"PRAGMA index_info({name})"))
            sigs[(t, cols)].append(name)
    return sigs


class TestFreshSchema:
    def test_no_two_indexes_on_the_same_columns(self, temp_db):
        dupes = {k: v for k, v in _index_signatures(temp_db._conn).items() if len(v) > 1}
        assert not dupes, (
            "同一组列上挂了多个索引，写入要多维护一份而查询只会用一个：\n" +
            "\n".join(f"  {t}{list(cols)} → {names}" for (t, cols), names in dupes.items())
        )

    @pytest.mark.parametrize("name", STALE_INDEX_NAMES)
    def test_stale_names_are_not_recreated(self, temp_db, name):
        """新库上这两个名字压根不该出现——建表代码里已经没有它们了。"""
        got = temp_db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name=?", (name,)
        ).fetchone()
        assert got is None, f"{name} 又被建回来了"


class TestExistingDatabaseIsCleaned:
    """存量库的迁移路径——新库测不出来这件事。

    只在「本次刚建表」的分支里 DROP 会漏掉所有存量库，而那恰恰是唯一有旧索引
    的那批。所以这里手动把旧索引造回去，再跑一次迁移。
    """

    def _reopen(self, path):
        """重开一个 Storage，并**强制重跑迁移**。

        ``StorageBase._migrated_paths`` 是进程级缓存：同一个路径在一个进程里
        只迁移一次。生产上换进程（重启/部署）自然会重跑，但测试在同一进程里
        反复开同一个文件，不清这个缓存的话第二次是空转——测的就不是迁移了。
        """
        from mstorage import Storage
        from mstorage._base import StorageBase

        StorageBase._migrated_paths.discard(str(path.resolve()))
        return Storage(path)

    def test_migration_drops_them(self, tmp_path):
        path = tmp_path / "legacy.db"
        st = self._reopen(path)
        with st._conn:
            st._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sc_changed_at "
                "ON status_changes(changed_at)")
            st._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sc_listing_id "
                "ON status_changes(listing_id)")
        present = {r[0] for r in st._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        assert set(STALE_INDEX_NAMES) <= present, "旧索引没造成功，这条用例就没意义"
        st.close()

        st2 = self._reopen(path)          # 重新打开 = 跑一遍迁移
        after = {r[0] for r in st2._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        assert not (set(STALE_INDEX_NAMES) & after), (
            f"存量库里的旧索引没被清掉: {sorted(set(STALE_INDEX_NAMES) & after)}")

    def test_the_kept_indexes_survive(self, tmp_path):
        """别把两个都删了——查询还要用。"""
        path = tmp_path / "legacy2.db"
        st = self._reopen(path)
        with st._conn:
            st._conn.execute("CREATE INDEX IF NOT EXISTS idx_sc_changed_at "
                             "ON status_changes(changed_at)")
        st.close()

        st2 = self._reopen(path)
        after = {r[0] for r in st2._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_status_changes_changed_at" in after
        assert "idx_status_changes_listing_id" in after

    def test_migration_is_idempotent(self, tmp_path):
        """DROP IF EXISTS 在没有旧索引的库上必须是无操作，不能抛。"""
        path = tmp_path / "clean.db"
        self._reopen(path).close()
        self._reopen(path).close()
        st = self._reopen(path)
        dupes = {k: v for k, v in _index_signatures(st._conn).items() if len(v) > 1}
        assert not dupes


class TestStatusChangesStillIndexed:
    """删重复不等于删覆盖——两条高频查询仍然要走索引。"""

    @pytest.mark.parametrize("sql,params", [
        ("SELECT * FROM status_changes WHERE listing_id=? ORDER BY changed_at", ("x",)),
        ("SELECT * FROM status_changes WHERE changed_at > ?", ("2026-01-01",)),
    ])
    def test_query_uses_an_index(self, temp_db, sql, params):
        plan = " ".join(
            str(r[-1]) for r in temp_db._conn.execute("EXPLAIN QUERY PLAN " + sql, params)
        )
        assert "USING INDEX" in plan or "USING COVERING INDEX" in plan, plan
