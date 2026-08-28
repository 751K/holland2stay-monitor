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

所以这里用和 scrapers/holland2stay.py 一样的判据：年份 >= 2050 视为哨兵，跳过。
「备份里有非空值」和「备份里有真实日期」不是一回事。

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

#: 与 scrapers/holland2stay.py 同一条判据：这一年及以后的都是「没有下一个合同
#: 起始日」的哨兵，不是真日期。按年份判而不是精确匹配 2050-01-01，哨兵换写法
#: （2099、2050-12-31）时不至于漏过去。
SENTINEL_YEAR = 2050


def _is_sentinel(value: str) -> bool:
    return bool(value) and value[:4].isdigit() and int(value[:4]) >= SENTINEL_YEAR


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
    if not missing:
        return 0

    files = sorted(
        f for f in glob.glob(str(DATA_DIR / "*.db"))
        if os.path.abspath(f) != os.path.abspath(str(live))
    )
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

    if not found:
        print("没有可恢复的。")
        return 0

    for lid, (af, src) in sorted(found.items(), key=lambda kv: kv[1][0]):
        print(f"  {missing[lid]['name'][:32]:<34}{missing[lid]['status'][:10]:<12}"
              f"{af}   ({src})")

    if not args.apply:
        print(f"\n这是预演。加 --apply 才会写入这 {len(found)} 条。")
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
    print(f"\n已写入 {n} 条。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
