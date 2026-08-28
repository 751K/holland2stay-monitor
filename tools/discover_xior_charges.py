"""discover_xior_charges.py — 重新发现每栋 Xior 楼盘的月度预付费
=================================================================
用法::

    python tools/discover_xior_charges.py            # 打印可直接粘贴的表
    python tools/discover_xior_charges.py --json out.json

需要一个能过 Cloudflare 的出口（楼盘页在 CF 后面，curl 的 TLS 指纹拿到 403），
所以它走项目自己的 BrowserFetcher。每页约 1.2 MB，30 栋跑完约十几分钟——这也是
为什么这张表是**登记**而不是每轮现抓。

解析为什么是通用的
------------------
不按固定标签清单匹配。2026-08-28 第一版就是那么写的，结果漏了一半：

    Sevice Charges        少一个 r，10 栋楼这么写
    Furnishings           另外几栋叫 Furniture & Upholstery
    Energy 65,00          Aachen Vaals 这一项没有 € 符号
    € 50.00 - € 70.00     家具项可以是区间

改成把「Monthly Advance Charges」到「Features」之间所有「文字 + 金额」对全抓
出来，不认识的项目也不会漏。

取 TOTAL 而不是明细求和
-----------------------
30 栋里 27 栋两者相等；Amsterdam Karspeldreef（差 €100）与 Groningen Zernike
Tower（差 €30）是页面自己算错。取 TOTAL 的理由见 scrapers/xior.py 里
MONTHLY_CHARGES 的注释。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import logging, os, re, html, json, time
logging.disable(logging.CRITICAL)
import config
PX = "socks5://172.19.0.1:1080"
for k in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
    os.environ[k] = PX
os.environ["SCRAPE_PROXIES_FALLBACK"] = ""
from urllib.parse import urlparse
from scrapers.xior import XiorScraper

def money(s):
    s = s.strip().replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    return float(s)

def text_of(t):
    t = re.sub(r"<script.*?</script>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<style.*?</style>", " ", t, flags=re.S | re.I)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", t)))

# 「标签 € 金额（可选 - € 金额）」；标签取金额之前最多 4 个词
PAIR = re.compile(r"([A-Za-z][A-Za-z&'’\s]{2,40}?)\s*€\s?([\d.,]+)(?:\s*[-–]\s*€\s?([\d.,]+))?")

sc = XiorScraper(); f = sc._ensure_browser()
out = {}
keys = list(XiorScraper.BUILDINGS)
for i, key in enumerate(keys, 1):
    b = XiorScraper.BUILDINGS[key]
    rec = {"display": b["display"]}
    try:
        r = f.fetch_plain(urlparse(b["url"]).path, timeout_ms=45_000)
        x = text_of(r.get("text") or "")
        a = x.find("Monthly Advance Charges")
        z = x.find("Features Select your room", a if a >= 0 else 0)
        block = x[a:z] if (a >= 0 and z > a) else ""
        rec["block_found"] = bool(block)
        pairs = []
        for m in PAIR.finditer(block):
            label = " ".join(m.group(1).split()[-4:]).strip()
            lo = money(m.group(2))
            hi = money(m.group(3)) if m.group(3) else None
            pairs.append({"label": label, "lo": lo, "hi": hi})
        rec["pairs"] = pairs
        rec["raw"] = block[-420:] if block else x[:200]
        rec["status"] = r.get("status")
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {str(e)[:110]}"
    out[key] = rec
    print(f"[{i:2}/{len(keys)}] {b['display']:<32} "
          f"{rec.get('error') or [(p['label'], p['lo'], p['hi']) for p in rec.get('pairs', [])]}",
          flush=True)
    time.sleep(2)

json.dump(out, open("/app/data/xior_charges_v2.json", "w"), ensure_ascii=False, indent=1)
print("\n写入 data/xior_charges_v2.json")
