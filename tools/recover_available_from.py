"""recover_available_from.py — 从历史备份里捞回被冲掉的入住日期
================================================================
用法::

    docker compose exec -T h2s python3 - < tools/recover_available_from.py
    docker compose exec -T h2s python3 - --apply < tools/recover_available_from.py

背景
----
mstorage/_listings.py 的 diff() 原来无条件写 available_from。H2S 房源转成
Reserved 之后上游给的是 2050-01-01 哨兵，scraper 认出哨兵返回 None，于是库里
那个真日期被 None 冲掉，界面上只剩「—」。

_sticky_available_from 已经堵住了源头，但堵不回已经丢的。data/ 下有一批
backup-*.db，能从里面把当时的值捞出来。

哨兵不能照搬回来
----------------
早于 2026-08-18 的备份里存的是**原始的 2050-01-01**——哨兵过滤是那之后才加进
scraper 的。直接把「备份里的非空值」写回去，等于把我们费劲滤掉的东西又写回库。

判据用 models.is_sentinel_available_from，和 scraper、存储层是同一个函数。
「备份里有非空值」和「备份里有真实日期」不是一回事。

顺带清理库里残留的哨兵
----------------------
同一批遗留还有另一半：2026-08-28 线上有 72 行 available_from 就是
``2050-01-01``，全是 H2S 的 Occupied，last_seen 全部停在 2026-08-18——加过滤
那天之后就再没被抓到过。它们**正显示在房源列表页上**，一个看起来像数据的假
日期，比「—」更糟；而且因为再也不会被抓到，粘性逻辑也永远碰不到它们。

这批没得救：备份最早只到 2026-08-19，那时候库里存的也已经是哨兵。所以置空，
让它显示「—」——不知道就说不知道。

取哪一份
--------
按备份文件名排序后取**第一个**出现真实日期的。备份是按时间命名的，越早的越接近
「这条房源还可订」的那个时刻，也就越可能是它真正的入住日。
"""
import sys
from pathlib import Path

try:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
except NameError:
    pass

import argparse
import glob
import os
import sqlite3

from config import DATA_DIR
from models import is_sentinel_available_from as _is_sentinel


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="实际写入；不加则只报告")
    args = ap.parse_args()

    live = DATA_DIR / "listings.db"
    con = sqlite3.connect(str(live))
    con.row_factory = sqlite3.Row
    missing = {
        r["id"]: dict(r)
        for r in con.execute(
            "SELECT id, name, status, source FROM listings "
            "WHERE available_from IS NULL OR available_from = ''"
        )
    }
    print(f"当前没有入住日期的房源  {len(missing)} 条")

    # 库里残留的哨兵。和「补回空值」是两件独立的事——早退会让其中一件永远
    # 不执行，而它们只是碰巧共用同一个判据。
    stale_ids = [
        r["id"] for r in con.execute(
            "SELECT id, available_from FROM listings "
            "WHERE available_from IS NOT NULL AND available_from != ''"
        ) if _is_sentinel(r["available_from"])
    ]
    print(f"库里存着哨兵本身        {len(stale_ids)} 条   （正显示在列表页上）")

    files = sorted(
        f for f in glob.glob(str(DATA_DIR / "*.db"))
        if os.path.abspath(f) != os.path.abspath(str(live))
    ) if missing else []
    print(f"扫描备份                {len(files)} 份")

    found: dict[str, tuple[str, str]] = {}
    sentinel_only: set[str] = set()
    for f in files:
        try:
            c = sqlite3.connect(f"file:{f}?mode=ro", uri=True)
            rows = c.execute(
                "SELECT id, available_from FROM listings "
                "WHERE available_from IS NOT NULL AND available_from != ''"
            ).fetchall()
            c.close()
        except Exception:
            continue        # 备份损坏 / schema 太老，跳过而不是中断整次恢复
        base = os.path.basename(f)
        for lid, af in rows:
            if lid not in missing or lid in found:
                continue
            af = (af or "").split(" ")[0].strip()
            if _is_sentinel(af):
                sentinel_only.add(lid)
                continue
            found[lid] = (af, base)
            sentinel_only.discard(lid)

    print(f"能捞回真实日期          {len(found)} 条")
    print(f"备份里也只有哨兵        {len(sentinel_only)} 条   （本来就没有真日期）")
    print(f"备份里查无此条          "
          f"{len(missing) - len(found) - len(sentinel_only)} 条\n")

    for lid, (af, src) in sorted(found.items(), key=lambda kv: kv[1][0]):
        print(f"  {missing[lid]['name'][:32]:<34}{missing[lid]['status'][:10]:<12}"
              f"{af}   ({src})")

    if not found and not stale_ids:
        print("没有需要处理的。")
        return 0

    if not args.apply:
        print(f"\n这是预演。加 --apply 会补 {len(found)} 条、"
              f"清 {len(stale_ids)} 条。")
        return 0

    with con:
        n = 0
        for lid, (af, _src) in found.items():
            cur = con.execute(
                "UPDATE listings SET available_from = ? "
                "WHERE id = ? AND (available_from IS NULL OR available_from = '')",
                (af, lid),
            )
            n += cur.rowcount
        cleared = 0
        if stale_ids:
            ph = ",".join("?" * len(stale_ids))
            cur = con.execute(
                f"UPDATE listings SET available_from = NULL WHERE id IN ({ph})",
                stale_ids,
            )
            cleared = cur.rowcount
    print(f"\n已补回 {n} 条，清掉 {cleared} 条哨兵。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
