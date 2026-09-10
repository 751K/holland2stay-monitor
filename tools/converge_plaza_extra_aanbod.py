"""converge_plaza_extra_aanbod.py — 把 Plaza「额外供给」房源静默收敛到 Not available
========================================================================================
用法::

    python tools/converge_plaza_extra_aanbod.py            # 只报告，不写（默认）
    python tools/converge_plaza_extra_aanbod.py --apply    # 实际写入

生产上代码打进镜像，通常喂 stdin 跑::

    docker compose exec -T h2s python3 - --apply < tools/converge_plaza_extra_aanbod.py

为什么需要这一次性收敛
----------------------
``scrapers/plaza.py`` 以前给每一条 Plaza 房源都写死 ``status="Available to book"``。
2026-09-10 登录实测发现，49 条荷兰住宅里 **31 条 ``isExtraAanbod`` 为真、服务端
``kanReageren`` 为 false**——只对被邀请的账号开放，普通用户点进去是「不能再应征」。
判据与旁证见 ``scrapers.plaza._EXTRA_AANBOD_NOTE``。

新版 scraper 会把这批报成 ``Not available``。**问题出在部署那一刻**：库里存的还是
``Available to book``，``diff()`` 一比就产出 31 条状态变化，订阅相关城市的用户会收到
一串「下架」通知。

那批通知**内容不假，但说的是 5 月发布的房源，而且下架这件事根本没发生**——变的是
我们的判据，不是上游的状态。把判据变更播成平台事件，是在用真实事件的通道发假事件。

做法：先把状态就地改掉，让 diff() 无事可做
--------------------------------------------
两条路都做，顺序有讲究：

1. **就地改 ``listings.status``**（主）。改完之后新版 scraper 抓回来的状态与库里
   一致，``diff()`` **压根不会产出事件**——没有事件就不需要压制事件。
2. **把已存在的未通知状态变化标成已通知**（兜底）。万一 monitor 在本脚本之前
   已经跑过一轮，事件已经躺在 ``status_changes`` 里了，这一步把它们收掉。

只做 2 不做 1 是不够的：``status_changes`` 里被标记的行不影响 ``listings.status``，
下一轮 diff() 会**再产出一次**同样的事件。只做 1 不做 2 则漏掉「已经跑过一轮」的情况。

部署顺序
--------
::

    supervisorctl -c /etc/supervisor/conf.d/app.conf stop monitor
    <部署新代码>
    <跑本脚本 --apply>
    supervisorctl -c /etc/supervisor/conf.d/app.conf start monitor

**先停 monitor 再部署**。如果在旧代码还在跑的时候就改库，monitor 下一轮会拿旧判据
把它们改回 ``Available to book``，反而多产出一次 ``Not available → Available to book``
的假「重新上架」——比不做还糟。

判据不在这个文件里
------------------
本脚本**不自己判断哪些是额外供给**，而是直接跑 ``PlazaScraper`` 拿解析结果，按它
给出的 status 来收敛。理由与 ``tools/backfill_push_optin.py`` 里那条一样：判据必须
是同一段代码，不是同一段描述。照着 ``isExtraAanbod`` 再抄一遍 WHERE，就等着两处慢慢
分叉——而分叉的表现是库里悄悄出现一批状态与 scraper 不一致的行，没有任何地方会报错。

代价是要联一次网（匿名接口，无需账号）。抓不到就退出，不猜。
"""
import sys
from pathlib import Path

try:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
except NameError:
    pass

import argparse
import sqlite3

from config import DATA_DIR

_BOOKABLE = "Available to book"


def _scraped_statuses() -> dict[str, str]:
    """跑一次 scraper，返回 ``{listing_id: status}``。抓不到就抛。"""
    from scrapers.plaza import PlazaScraper

    scraper = PlazaScraper()
    items, complete = scraper._parse_all(scraper._fetch())
    if not complete:
        raise RuntimeError(
            "Plaza 响应没通过完整性探针——这次拿到的不是一份可信的接口响应，"
            "不能拿它去改库。稍后重试。")
    if not items:
        raise RuntimeError("Plaza 解析出 0 条，拒绝据此改库。")
    return {i.id: i.status for i in items}


def main() -> int:
    ap = argparse.ArgumentParser(description="把 Plaza 额外供给房源静默收敛")
    ap.add_argument("--apply", action="store_true", help="实际写入；不加则只报告")
    args = ap.parse_args()

    scraped = _scraped_statuses()
    demoted = {lid for lid, st in scraped.items() if st != _BOOKABLE}
    print(f"scraper 本轮解析      {len(scraped)} 条，其中不可应征 {len(demoted)} 条")
    if not demoted:
        print("没有需要收敛的房源。")
        return 0

    con = sqlite3.connect(str(DATA_DIR / "listings.db"))
    con.row_factory = sqlite3.Row

    ph = ",".join("?" for _ in demoted)
    ids = list(demoted)
    stale = [
        dict(r) for r in con.execute(
            f"SELECT id, name, status FROM listings "
            f"WHERE id IN ({ph}) AND status != ?", ids + ["Not available"])
    ]
    pending = [
        r["listing_id"] for r in con.execute(
            f"SELECT DISTINCT listing_id FROM status_changes "
            f"WHERE listing_id IN ({ph}) AND notified = 0", ids)
    ]

    print(f"库里状态需要改的      {len(stale)} 条")
    for r in stale[:5]:
        print(f"    {r['id']}  {r['status']!r} → 'Not available'  {r['name'][:44]}")
    if len(stale) > 5:
        print(f"    …… 另外 {len(stale) - 5} 条")
    print(f"待通知的状态变化      {len(pending)} 条（monitor 若已跑过一轮才会有）")

    if not args.apply:
        print("\n未写入。确认无误后加 --apply。")
        return 0

    with con:
        cur = con.execute(
            f"UPDATE listings SET status = ? WHERE id IN ({ph}) AND status != ?",
            ["Not available"] + ids + ["Not available"])
        changed = cur.rowcount
        cur = con.execute(
            f"UPDATE status_changes SET notified = 1 "
            f"WHERE listing_id IN ({ph}) AND notified = 0", ids)
        suppressed = cur.rowcount

    print(f"\n已改状态              {changed} 条")
    print(f"已压制的未通知变化    {suppressed} 条")
    print("现在启动 monitor：diff() 与库里一致，不会产出事件。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
