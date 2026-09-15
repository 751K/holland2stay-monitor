"""ourcampus_booker_probe.py — 用真实单元验一次 OurCampusBooker
================================================================
**在你自己的终端里跑。密码用 getpass 读，不落盘、不进 shell 历史、不回显。**

    python tools/ourcampus_booker_probe.py --listing oc_456556            # 默认：只预检
    python tools/ourcampus_booker_probe.py --listing oc_456556 --start    # 真的开始申请

``tools/`` 不在镜像里。要用服务器的代理池和 ``CAPTCHA_API_KEY``，就临时拷进容器::

    docker compose cp tools/ourcampus_booker_probe.py h2s:/tmp/probe.py
    docker compose exec -it -e PYTHONPATH=/app h2s python /tmp/probe.py --listing oc_XXXX

房源 id 只在 OC 放房时存在，没有默认值。

默认只预检，不需要账号
----------------------
走 ① 开 floorplans.aspx ② POST 取单元表并找到这个单元、判定它是不是直接预订
③ 点 Book Now（把单元写进会话，落到条款页），然后核对条款页：表单在不在、
``UnitId`` 是不是这个单元、reCAPTCHA 配置与 ``captcha/rentcafe_pages.py`` 是否一致。

**不解验证码、不登录、不在任何账号下留下东西**——OurDomain 上实测这三步不建
申请记录。它回答的是「我们的代码能不能从一条 oc_ 房源走到 Start Application
前一刻」，而 OC 这条路至今一次都没用真实响应跑过（09-15 刚发现单元表的
onclick 用双引号，旧解析器对 OC 每一行都解析不出参数）。

``--start`` 会真的开始申请
--------------------------
解验证码（v3 大概率被拒、回退 v2，约 100 秒，花 2Captcha 的钱）→ 登录 → 进到
Applicant Info 停下。服务端会在你的账号下建出一份申请（ProspectId），和你在
网站上点 Start Application 一样。要求再手打一次房源 id 确认。

跑完之后要做的两件事（这才是 --start 的目的）
--------------------------------------------
1. 记下时间，对照 ``data/ourcampus_capture.txt`` 看这个单元什么时候从匿名列表
   消失、会不会再出现——回答「开始申请能不能占住单元」。
2. 换一台设备登录 OC，看这份申请在不在、能不能续填。
"""
import sys
from pathlib import Path

try:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
except NameError:
    pass

import argparse
import getpass
import logging
import os
import re
from datetime import datetime, timezone


def _preflight(listing) -> int:
    import functools

    from bookers.rentcafe import (
        OurCampusBooker, RentCafeNotBookableError, RentCafeSession, _extract_form_fields,
        terms_page_notices,
    )
    from bookers.rentcafe_units import find_unit, parse_apply_now_options
    from captcha.rentcafe_pages import RENTCAFE_PAGES

    booker = OurCampusBooker()
    building = booker._building(listing)
    session = RentCafeSession("", source=booker.source)

    lottery_note = ""
    try:
        unit = booker._open_and_find_unit(session, listing, building)
    except RentCafeNotBookableError as exc:
        # 预检没有副作用，抽签单元也照样走到条款页——那页上写着「提交意味着
        # 什么」，正是要看的东西。booker 真跑时会在这里停。
        lottery_note = str(exc)
        from scrapers.ourdomain import _extract_floorplan_ids

        parser = functools.partial(parse_apply_now_options, id_prefix=booker.id_prefix)
        unit = None
        for fp in _extract_floorplan_ids(session.current_page_html()):
            html = booker._fetch_units_html(
                session, fp_id=fp, move_in="", property_id="",
                floorplans_url=session.ole_url("floorplans.aspx"))
            unit = find_unit(html, listing.id, id_prefix=booker.id_prefix, parser=parser)
            if unit:
                break
    print(f"  ① floorplans.aspx 已打开（指纹 {session.impersonate}）")
    if unit is None:
        print("  ② 单元表里没有这个单元（已下架，或 id 不对）")
        return 0
    print(f"  ② 找到 {unit.label}：floorplan={unit.floor_plan_id} "
          f"入住={unit.available_date} → {unit.next_url}")
    if lottery_note:
        print(f"     ⚠️ booker 会在这里停：{lottery_note}")

    html = session.open_terms_for_unit(unit)
    fields = _extract_form_fields(html, "termsandotheritems", required=False)
    print(f"  ③ 条款页 {len(html)} 字节，form#termsandotheritems "
          f"{'在' if fields else '不在'}，字段 {len(fields)} 个")

    problems = []
    if not fields:
        problems.append("条款页上没有 form#termsandotheritems")
    if fields.get("UnitId") != unit.unit_id:
        problems.append(f"UnitId={fields.get('UnitId')!r}，不是 {unit.unit_id}——单元上下文没写进会话")
    want = RENTCAFE_PAGES["termsandotheritems"]
    if want.v3_sitekey not in html:
        problems.append("v3 sitekey 与 rentcafe_pages 不一致")
    if not re.search(rf"action\s*:\s*['\"]{re.escape(want.action)}['\"]", html):
        problems.append(f"没找到 action={want.action}")
    if want.fallback_flag and want.fallback_flag not in html:
        problems.append(f"没找到回退字段 {want.fallback_flag}")

    notices = terms_page_notices(html)
    print("\n  条款页说明（提交之后意味着什么）：")
    for n in notices or ["（没有找到含 reserve / lottery / hold 等词的句子）"]:
        print(f"    · {n}")

    if problems:
        print("\n❌ 预检没过：")
        for p in problems:
            print(f"   - {p}")
        return 2
    print("\n✅ 预检通过：到 Start Application 前一刻为止，全部与真实页面对得上。")
    return 0


def _start(listing) -> int:
    from bookers.base import BookingRequest
    from bookers.rentcafe import OurCampusBooker
    from config import AutoBookConfig
    from users import UserConfig

    if not os.environ.get("CAPTCHA_API_KEY"):
        print("没有 CAPTCHA_API_KEY，条款页那一步必然失败。到服务器容器里跑。")
        return 1

    print("OurCampus 账号")
    email = input("  邮箱: ").strip()
    password = getpass.getpass("  密码（不回显）: ")
    if not (email and password):
        print("没填，退出。")
        return 1

    print(f"\n⚠️  --start 会在你的账号下**真的开始申请** {listing.id}。")
    if input("    再打一次房源 id 确认: ").strip() != listing.id:
        print("对不上，取消。")
        return 1

    user = UserConfig(name="probe")
    user.auto_book = AutoBookConfig(
        enabled=True, dry_run=False, ourcampus_email=email, ourcampus_password=password)

    started = datetime.now(timezone.utc)
    print(f"\n开始：{started.isoformat(timespec='seconds')}（验证码回退 v2 时约 100 秒）")
    result = OurCampusBooker().book(BookingRequest(listing=listing, user=user))
    ended = datetime.now(timezone.utc)

    print("\n── 结果 ──────────────────────────────")
    print(f"  success : {result.success}")
    print(f"  phase   : {result.phase}")
    print(f"  message : {result.message}")
    print(f"  用时    : {(ended - started).total_seconds():.0f} 秒，"
          f"结束于 {ended.isoformat(timespec='seconds')}")
    print("──────────────────────────────────────")

    if result.phase == "application_started":
        print(
            "\n接下来两件事：\n"
            f"  1. 对照 data/ourcampus_capture.txt，看 {listing.id} 在 "
            f"{ended.strftime('%H:%M:%S')} UTC 之后是否从列表里消失、会不会再出现\n"
            "  2. 换一台设备登录 OC，看这份申请在不在、能不能续填"
        )
    return 0 if result.success else 2


def main() -> int:
    ap = argparse.ArgumentParser(description="OurCampusBooker 真实单元验证")
    ap.add_argument("--listing", required=True, help="房源 id（带 oc_ 前缀）")
    ap.add_argument("--start", action="store_true",
                    help="真的开始申请（解验证码 + 登录）。不加则只预检，不需要账号")
    ap.add_argument("-v", "--verbose", action="store_true", help="打开 DEBUG 日志")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s")

    if not args.listing.startswith("oc_"):
        print("房源 id 要带 oc_ 前缀。")
        return 1

    from models import Listing
    from scrapers.ourcampus import OurCampusScraper

    meta = OurCampusScraper.BUILDINGS["diemen"]
    listing = Listing(
        id=args.listing, name=f"probe {args.listing}", status="Available to book",
        price_raw="", available_from=None, features=[],
        url=f"{OurCampusScraper.BASE}/{meta['slug']}/floorplans.aspx",
        city=meta["display"], source="ourcampus")

    if args.start:
        return _start(listing)
    print(f"预检 {args.listing}（不解验证码、不登录）")
    return _preflight(listing)


if __name__ == "__main__":
    raise SystemExit(main())
