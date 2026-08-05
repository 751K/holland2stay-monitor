"""观测一张 cf_clearance 能复用多久。**只读，不动任何生产状态。**

背景
----
Cloudflare 的挑战载荷是代理流量里最大的一项（2026-08-04：985MB 中 558MB，
56.6%），而它每次重建浏览器都要重下一遍。

2026-08-05 实测发现：浏览器过一次挑战之后，把 ``cf_clearance`` 与 UA 交给
curl_cffi、走同一个出口 IP，GraphQL 直接返回 200。也就是说「过挑战」需要浏览器，
「发请求」不需要。

若一张 clearance 能复用数小时，浏览器就只需在过挑战时存在几秒，重建频率也能与
「保持会话」解耦——那才是省流量的地方。但**真实寿命由 Cloudflare 服务端决定**，
cookie 上标称的一年靠不住。本脚本就是去测那个数。

做法
----
开一次浏览器过挑战 → 导出 cookie 与 UA → 关掉浏览器 → 之后只用 curl_cffi
定时探同一个 GraphQL 端点，记录每次的状态码，直到失效或到时。

每次探测是一个只取 ``total_count`` 的最小查询，几百字节；按 5 分钟一次、跑 24
小时算，总量不到 1MB。

用法
----
    python -m tools.clearance_probe --interval 300 --hours 24 \\
        --out data/clearance_probe.jsonl

注意
----
- 用的是**独立的**代理 session（``probe`` 而非 ``holland2stay``），不会占用也不会
  干扰生产抓取的出口 IP。
- 只发 GraphQL 读查询，不写任何东西。
- 结果逐行 JSON 追加，中途 kill 掉也不丢已有数据。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# 仓库里它在 tools/ 下；镜像里 Dockerfile 不 COPY tools/，实际是被拷到 /app 根下
# 单独跑的。两种位置都要能找到 browser_fetcher。
_here = Path(__file__).resolve().parent
for _cand in (_here.parent, _here):
    if (_cand / "browser_fetcher.py").exists():
        sys.path.insert(0, str(_cand))
        break

logger = logging.getLogger("clearance_probe")

#: 只取 total_count，不翻页不取字段——探的是「这张票还认不认」，不是数据本身。
PROBE_QUERY = {
    "query": '{products(filter:{category_uid:{eq:"Nw=="}},pageSize:1){total_count}}',
    "variables": {},
}
GQL_URL = "https://www.holland2stay.com/api/graphql"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def acquire_clearance(source: str = "probe") -> tuple[dict, str, str]:
    """开一次浏览器过挑战，返回 ``(cookies, user_agent, proxy_url)``。

    proxy_url 必须原样带出去——clearance 绑出口 IP，重新调 get_proxy_url() 在
    rotating 的 profile 上会拿到另一个 session，那张票就作废了。
    """
    from browser_fetcher import BrowserFetcher

    with BrowserFetcher() as f:
        f.ensure_initialized()
        cookies = {c["name"]: c["value"] for c in f._page.context.cookies()}
        ua = f._page.evaluate("() => navigator.userAgent")
        proxy = f._proxy_url
    return cookies, ua, proxy


def probe(cookies: dict, ua: str, proxy: str, impersonate: str = "chrome131") -> dict:
    """探一次，返回一条可直接落盘的记录。异常也记录，不抛。"""
    from curl_cffi import requests as req

    proxies = {"http": proxy, "https": proxy} if proxy else None
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Store": "default",
        "Content-Currency": "EUR",
        "User-Agent": ua,
        "Referer": "https://www.holland2stay.com/residences",
    }
    started = time.monotonic()
    try:
        r = req.post(GQL_URL, json=PROBE_QUERY, headers=headers, cookies=cookies,
                     impersonate=impersonate, proxies=proxies, timeout=30)
        body = r.text[:200]
        ok = r.status_code == 200 and '"data"' in body
        return {
            "at": _now(), "status": r.status_code, "ok": ok,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "impersonate": impersonate,
            # 失败时留一段正文——403 到底是挑战页还是别的，事后要能分辨
            "body": None if ok else body.replace("\n", " ")[:160],
        }
    except Exception as e:
        return {
            "at": _now(), "status": None, "ok": False,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "impersonate": impersonate,
            "body": f"{type(e).__name__}: {e}"[:160],
        }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--interval", type=int, default=300, help="探测间隔秒数，默认 300")
    ap.add_argument("--hours", type=float, default=24.0, help="最长跑多久，默认 24 小时")
    ap.add_argument("--out", default="data/clearance_probe.jsonl")
    ap.add_argument("--impersonate", default="chrome131")
    ap.add_argument(
        "--stop-after-failures", type=int, default=3,
        help="连续失败几次就停。默认 3——单次失败可能只是网络抖动，"
             "连续三次才说明这张票真的不认了",
    )
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    logger.info("取 clearance（会开一次浏览器）...")
    cookies, ua, proxy = acquire_clearance()
    clr = cookies.get("cf_clearance", "")
    if not clr:
        logger.error("没拿到 cf_clearance，放弃")
        return 1
    acquired_at = time.monotonic()

    from browser_fetcher import _redact_proxy
    header = {
        "at": _now(), "event": "acquired",
        "cf_clearance_len": len(clr),
        "cookies": sorted(cookies),
        "ua": ua,
        "proxy": _redact_proxy(proxy) if proxy else "",
        "interval_sec": args.interval,
        "impersonate": args.impersonate,
    }
    with out.open("a") as f:
        f.write(json.dumps(header, ensure_ascii=False) + "\n")
    logger.info("已取得 clearance（%d 字节），浏览器已关闭；开始定时探测", len(clr))

    deadline = acquired_at + args.hours * 3600
    fails = 0
    n = 0
    while time.monotonic() < deadline:
        rec = probe(cookies, ua, proxy, args.impersonate)
        rec["age_min"] = round((time.monotonic() - acquired_at) / 60, 1)
        n += 1
        with out.open("a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        if rec["ok"]:
            fails = 0
            logger.info("#%d  存活 %.1f 分钟  HTTP %s", n, rec["age_min"], rec["status"])
        else:
            fails += 1
            logger.warning(
                "#%d  存活 %.1f 分钟  失败 %d/%d  HTTP %s  %s",
                n, rec["age_min"], fails, args.stop_after_failures,
                rec["status"], rec["body"],
            )
            if fails >= args.stop_after_failures:
                logger.info("连续 %d 次失败，判定 clearance 已失效，存活约 %.1f 分钟",
                            fails, rec["age_min"])
                with out.open("a") as f:
                    f.write(json.dumps({
                        "at": _now(), "event": "expired",
                        "survived_min": rec["age_min"], "probes": n,
                    }, ensure_ascii=False) + "\n")
                return 0
        time.sleep(args.interval)

    logger.info("到时仍然存活：%.1f 小时内共探测 %d 次，全部通过", args.hours, n)
    with out.open("a") as f:
        f.write(json.dumps({
            "at": _now(), "event": "still_alive",
            "survived_min": round((time.monotonic() - acquired_at) / 60, 1),
            "probes": n,
        }, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
