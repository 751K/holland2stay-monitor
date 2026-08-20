"""一次性回填 H2S 房源的详情字段（楼盘 / 租客资格 / 片区 / 收入要求）。

为什么需要它
------------
这几个字段不是列表抓取产出的，是另发一次 ``GetProductDetail`` 补齐的
（白名单主查询的字段集里没有它们，见 docs/H2S.md §5.6）。而常规补齐**只跑在
当轮抓到的房源上**：一条房源如果在被抓到的那一轮恰好撞上 429、没轮到补齐，
之后又掉出了 feed，就再也没有机会补——库里就留下一条永远显示「—」的陈旧行。

本脚本把这些陈旧行一次性补齐。之后新房源由 scrapers/holland2stay.py 的常规
补齐覆盖，不需要再跑。

用法
----
必须在容器里跑（要 CloakBrowser 过 Cloudflare 挑战）::

    docker compose exec -T h2s python3 tools/backfill_h2s_details.py --dry-run
    docker compose exec -T h2s python3 tools/backfill_h2s_details.py

选项::

    --dry-run     只统计要补多少条，不发请求、不写库
    --limit N     最多补 N 条（分批跑用）
    --spacing S   两次请求间隔秒数，默认跟随抓取侧的 _DETAIL_REQUEST_SPACING

限流
----
H2S 按**速率**限流（实测连发 ~26 条即 429）。脚本在每次请求之间等待，并在撞到
429 时指数退避重试；连续失败过多则中止，避免把出口 IP 撞进更严的限制。

幂等
----
只挑「缺 Building 或缺 Tenant」的行，补上就不会再被选中。中途失败可以直接重跑。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DB = Path(__file__).resolve().parent.parent / "data" / "listings.db"

#: 连续失败多少次就中止。撞墙还硬打只会让限流更严。
_MAX_CONSECUTIVE_FAILURES = 5

#: 429 后的退避序列（秒）。比抓取侧激进，因为这是一次性任务、可以慢慢来。
_BACKOFF = (10, 30, 60, 120)


def _rows_needing_backfill(conn) -> list[tuple[str, str, str]]:
    """返回 (id, name, features_json)，只挑缺粘性字段的 H2S 行。"""
    cur = conn.execute(
        """SELECT id, name, features FROM listings
           WHERE source = 'holland2stay'
             AND (features NOT LIKE '%Building:%' OR features NOT LIKE '%Tenant:%')
           ORDER BY last_seen DESC"""
    )
    return [(r[0], r[1], r[2]) for r in cur.fetchall()]


def _merged_features(old_json: str, extra: dict[str, str]) -> list[str] | None:
    """把补齐结果并进旧 features；没有变化时返回 None。"""
    try:
        old = json.loads(old_json) if old_json else []
    except json.JSONDecodeError:
        old = []
    if not isinstance(old, list):
        old = []

    have = {f.split(":", 1)[0].strip() for f in old if isinstance(f, str) and ":" in f}
    added = [f"{k}: {v}" for k, v in extra.items() if k not in have]
    if not added:
        return None
    return old + added


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="只统计，不发请求不写库")
    ap.add_argument("--limit", type=int, default=0, help="最多补 N 条（0 = 不限）")
    ap.add_argument("--spacing", type=float, default=0.0,
                    help="请求间隔秒数（默认跟随抓取侧配置）")
    args = ap.parse_args()

    import sqlite3

    conn = sqlite3.connect(DB)
    try:
        rows = _rows_needing_backfill(conn)
    finally:
        pass

    total_h2s = conn.execute(
        "SELECT COUNT(*) FROM listings WHERE source='holland2stay'").fetchone()[0]
    print(f"H2S 房源共 {total_h2s} 条，其中缺详情字段的 {len(rows)} 条")

    if args.limit:
        rows = rows[: args.limit]
        print(f"本次只处理前 {len(rows)} 条（--limit）")

    if args.dry_run:
        for lid, name, _ in rows[:20]:
            print(f"  待补 {name[:40]:42s} ({lid})")
        if len(rows) > 20:
            print(f"  … 另有 {len(rows) - 20} 条")
        print("\n--dry-run：未发任何请求、未写库")
        conn.close()
        return

    if not rows:
        print("没有需要回填的行。")
        conn.close()
        return

    # 延迟导入：这些会拉起 CloakBrowser，dry-run 不该付这个代价
    from browser_fetcher import BrowserFetcher
    from config import CLOAKBROWSER_HEADLESS
    from scrapers.base import RateLimitError
    from scrapers.holland2stay import (
        _DETAIL_REQUEST_SPACING, _fetch_detail,
    )

    spacing = args.spacing or _DETAIL_REQUEST_SPACING
    print(f"请求间隔 {spacing}s，撞 429 退避 {_BACKOFF}\n")

    filled = skipped = failed = 0
    consecutive_failures = 0

    with BrowserFetcher(headless=CLOAKBROWSER_HEADLESS) as fetcher:
        fetcher.ensure_initialized()

        for i, (lid, name, old_json) in enumerate(rows, 1):
            if i > 1:
                time.sleep(spacing)

            extra = None
            for attempt, wait in enumerate((0,) + _BACKOFF):
                if wait:
                    print(f"    429，退避 {wait}s 后重试（第 {attempt} 次）")
                    time.sleep(wait)
                try:
                    extra = _fetch_detail(fetcher, lid)
                    break
                except RateLimitError:
                    continue
                except Exception as e:
                    print(f"  ✗ {name[:38]:40s} {type(e).__name__}: {e}")
                    extra = None
                    break

            if extra is None:
                failed += 1
                consecutive_failures += 1
                if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    print(f"\n连续 {consecutive_failures} 条失败，中止。"
                          f"稍后重跑即可（脚本幂等）。")
                    break
                continue

            consecutive_failures = 0

            merged = _merged_features(old_json, extra)
            if merged is None:
                skipped += 1
                print(f"  · {name[:38]:40s} 上游也没有这些字段，跳过")
                continue

            conn.execute(
                "UPDATE listings SET features=? WHERE id=?",
                (json.dumps(merged, ensure_ascii=False), lid),
            )
            conn.commit()
            filled += 1
            shown = " ".join(f"{k}={v}" for k, v in extra.items())
            print(f"  ✓ [{i}/{len(rows)}] {name[:32]:34s} {shown[:70]}")

    conn.close()
    print(f"\n完成：补齐 {filled}，上游无数据 {skipped}，失败 {failed}")
    if failed:
        print("失败的行下次重跑会再试（脚本幂等）。")


if __name__ == "__main__":
    main()
