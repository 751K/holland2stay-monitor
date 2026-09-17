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


def _start(listing, *, even_if_lottery: bool = False) -> int:
    from bookers.base import BookingRequest
    from bookers.rentcafe import OurCampusBooker
    from config import AutoBookConfig
    from users import UserConfig

    def _describe(html: str) -> None:
        """登录后落在哪儿了。

        booker 只会说「没落到 Applicant Info」——**落在哪**才是要查的东西。
        账号已经申请过这条房源时，服务端不会让你再进填表页，而那是正常状态，
        不是故障（Plaza 那边对应 WINKEL-REACTIE-DUBBEL）。
        """
        import re as _re
        from html import unescape as _unescape

        if not html:
            print("   落地页：空响应")
            return
        title = _re.search(r"<title[^>]*>(.*?)</title>", html, _re.S | _re.I)
        print(f"   落地页 {len(html)} 字节，title={(title.group(1).strip() if title else '?')[:80]!r}")
        steps = sorted(set(_re.findall(r"stepname=([A-Za-z]+)", html)))
        print("   步骤参数：", steps or "（无）")
        forms = sorted(set(_re.findall(r"<form[^>]*\bid=['\"]([^'\"]+)", html)))
        print("   表单：", forms or "（无）")
        text = _re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=_re.S | _re.I)
        text = _unescape(_re.sub(r"<[^>]+>", "\n", text))
        kw = _re.compile(r"already|existing|in progress|pending|applied|appli|lotter|"
                         r"cannot|unable|error|sorry|contact", _re.I)
        seen = set()
        hits = []
        for line in text.split("\n"):
            line = " ".join(line.split())
            if 15 < len(line) < 240 and kw.search(line) and line not in seen:
                seen.add(line)
                hits.append(line)
        print("   页面上相关的话：")
        for h in hits[:12]:
            print("     ·", h)
        if not hits:
            print("     （没找到 already / applied / error 一类的句子）")
        # 落地页很小 = 多半是壳响应/跳转片段，光看特征猜不出来，原样打出来。
        import pathlib as _pl
        dump = _pl.Path("/tmp/oc_landing.html")
        try:
            dump.write_text(html, encoding="utf-8")
            print(f"   原始 HTML 已存到 {dump}")
        except OSError:
            pass
        if len(html) <= 4000:
            print("   ── 原文 ──")
            print("   " + html.strip().replace("\n", "\n   ")[:2500])
            print("   ── 原文结束 ──")

    booker_cls = OurCampusBooker
    if even_if_lottery:
        # OC 2026-09 起全站抽签，booker 在登录**之前**就会停下——那样验证不到登录。
        # 只在探针里摘掉这道闸：booker 本身不动，被绕过的判据只影响这一次运行。
        # 仍然只走到 Applicant Info，不提交；按站点的说法「提交申请」才算进抽签池。
        class booker_cls(OurCampusBooker):  # noqa: N801
            def _check_still_direct_booking(self, html, unit):
                return None

    # 先 import config：服务器上这个键在 .env 里，load_dotenv 之后才进 os.environ。
    # 不先加载就会误报「没配置」，而容器里其实是有的。
    import config  # noqa: F401

    if not os.environ.get("CAPTCHA_API_KEY"):
        print("没有 CAPTCHA_API_KEY，条款页那一步必然失败。到服务器容器里跑。")
        return 1

    print("OurCampus 账号")
    email = input("  邮箱: ").strip()
    password = getpass.getpass("  密码（不回显）: ")
    if not (email and password):
        print("没填，退出。")
        return 1

    what = ("开始申请（抽签房：**不提交**，所以不会替你报名，"
            "但账号下会多一条进行中的申请）" if even_if_lottery else "开始申请")
    print(f"\n⚠️  --start 会在你的账号下**真的{what}** {listing.id}。")
    if input("    再打一次房源 id 确认: ").strip() != listing.id:
        print("对不上，取消。")
        return 1

    user = UserConfig(name="probe")
    user.auto_book = AutoBookConfig(
        enabled=True, dry_run=False, ourcampus_email=email, ourcampus_password=password)

    # 抓住 session：失败时要看落地页，而 book() 只返回一句话。
    holder: dict = {}
    _orig_reach = booker_cls._reach_applicant_info

    def _reach(self, session, listing_, email_, password_):
        holder["session"] = session
        return _orig_reach(self, session, listing_, email_, password_)

    booker_cls._reach_applicant_info = _reach

    started = datetime.now(timezone.utc)
    print(f"\n开始：{started.isoformat(timespec='seconds')}（验证码回退 v2 时约 100 秒）")
    result = booker_cls().book(BookingRequest(listing=listing, user=user))
    ended = datetime.now(timezone.utc)

    print("\n── 结果 ──────────────────────────────")
    print(f"  success : {result.success}")
    print(f"  phase   : {result.phase}")
    print(f"  message : {result.message}")
    print(f"  用时    : {(ended - started).total_seconds():.0f} 秒，"
          f"结束于 {ended.isoformat(timespec='seconds')}")
    print("──────────────────────────────────────")

    if result.phase != "application_started":
        session = holder.get("session")
        print("\n── 登录之后落在哪儿 ──────────────────")
        if session is None:
            print("   还没走到登录就结束了")
        else:
            _describe(session.current_page_html())
        print("──────────────────────────────────────")

    if result.phase == "application_started":
        print(
            "\n接下来两件事：\n"
            f"  1. 对照 data/ourcampus_capture.txt，看 {listing.id} 在 "
            f"{ended.strftime('%H:%M:%S')} UTC 之后是否从列表里消失、会不会再出现\n"
            "  2. 换一台设备登录 OC，看这份申请在不在、能不能续填"
        )
    return 0 if result.success else 2


def _diagnose(listing) -> int:
    """登录之后、重选之前，到底落在哪一页。

    booker 现在的兜底是「登录后不是 Applicant Info 就把单元重选一遍」，而实测服务端
    对重选回的是 ``Unit is not available. Please select another unit.``——那条消息是
    **兜底动作自己造成的**，不是单元真没了（同一时刻抓取侧还列着 16 套）。

    所以这里不走兜底，只把三样东西摊开：登录后的落地页、重新 GET 一次
    oleapplication.aspx、以及（若能取到 ProspectId）按 XIOR.md §8.7 的形状去拉
    ApplicantInfo 那段内容。**全程不提交任何东西。**
    """
    import functools
    import getpass
    import pathlib
    import re as _re

    import config  # noqa: F401  —— .env 里的 CAPTCHA_API_KEY 要先加载

    # 正则放在 f-string 外面：容器是 Python 3.11，f-string 的表达式里不能有反斜杠。
    _PID_RE = _re.compile(r"""ProspectId['"]?\s*[:=]\s*['"]([^'"&]+)""")
    _TITLE_RE = _re.compile(r"<title[^>]*>(.*?)</title>", _re.S | _re.I)
    _FORM_ID_RE = _re.compile(r"""<form[^>]*\bid=['"]([^'"]+)""")
    _MSG_RE = _re.compile(r'showMessage\(\{[^}]*text:\s*"([^"]+)"')

    from bookers.rentcafe import (
        OurCampusBooker, RentCafeSession, _extract_form_fields,
    )
    from bookers.rentcafe_form import parse_applicant_form
    from bookers.rentcafe_units import find_unit, parse_apply_now_options
    from scrapers.ourdomain import _extract_floorplan_ids

    if not os.environ.get("CAPTCHA_API_KEY"):
        print("没有 CAPTCHA_API_KEY。到服务器容器里跑。")
        return 1

    print("OurCampus 账号")
    email = input("  邮箱: ").strip()
    password = getpass.getpass("  密码（不回显）: ")
    if not (email and password):
        print("没填，退出。")
        return 1

    def dump(tag: str, html: str) -> None:
        path = pathlib.Path(f"/tmp/oc_{tag}.html")
        try:
            path.write_text(html or "", encoding="utf-8")
        except OSError:
            pass
        body = html or ""
        form = parse_applicant_form(body)
        pid_m = _PID_RE.search(body)
        title_m = _TITLE_RE.search(body)
        title = title_m.group(1).strip()[:70] if title_m else "?"
        forms = sorted(set(_FORM_ID_RE.findall(body)))
        msgs = sorted(set(_MSG_RE.findall(body)))
        size = len(body)
        print(f"\n── {tag}：{size} 字节 → {path}")
        print("   title=" + repr(title))
        print("   表单=" + str(forms))
        parsed = "成功（ProspectId=" + form.prospect_id + "）" if form else "失败"
        print("   ApplicantForm 解析=" + parsed)
        print("   页面里的 ProspectId=" + (pid_m.group(1) if pid_m else "（没有）"))
        if msgs:
            print("   服务端消息=" + str(msgs))

    booker = OurCampusBooker()
    building = booker._building(listing)
    session = RentCafeSession(os.environ["CAPTCHA_API_KEY"], source="ourcampus")
    session.open(booker._floorplans_url(building))

    parser = functools.partial(parse_apply_now_options, id_prefix=booker.id_prefix)
    fps = _extract_floorplan_ids(session.current_page_html())
    print(f"   floorplans 页 {len(session.current_page_html())} 字节，房型 id={fps}")
    unit = None
    for fp in fps:
        html = booker._fetch_units_html(
            session, fp_id=fp, move_in="", property_id="",
            floorplans_url=session.ole_url("floorplans.aspx"))
        opts = parser(html)
        print(f"   fp={fp}：{len(html)} 字节，解析出 {len(opts)} 个单元"
              + (" → " + ", ".join(o.listing_id for o in opts[:20]) if opts else ""))
        unit = find_unit(html, listing.id, id_prefix=booker.id_prefix, parser=parser)
        if unit:
            break
    if unit is None:
        print("单元表里没有这个单元（已下架，或 id 不对）")
        return 0
    print(f"① 选中 {unit.label}（fp={unit.floor_plan_id}）")

    terms_html = session.open_terms_for_unit(unit)
    fields = _extract_form_fields(terms_html, "termsandotheritems")
    print(f"② 条款页 {len(terms_html)} 字节，{len(fields)} 个字段")
    after_terms = session.submit_terms(fields, page="termsandotheritems")
    dump("after_terms", after_terms)

    session.login(email, password,
                  landed=lambda h: parse_applicant_form(h) is not None,
                  landing_url=session.ole_url("oleapplication.aspx"))
    dump("after_login", session.current_page_html())

    fresh = session.fetch(session.ole_url("oleapplication.aspx"),
                          referer=session.ole_url("oleapplication.aspx"))
    dump("oleapplication_get", fresh)

    # XIOR.md §8.7：后续步骤的内容走 rcLoadContent，缺 ProspectId 一律 500。
    pid = ""
    for src in (session.current_page_html(), fresh):
        m = _PID_RE.search(src or "")
        if m:
            pid = m.group(1)
            break
    if pid:
        url = session.content_url(
            f"contentclass=ApplicantInfo&stepname=ApplicantInfo"
            f"&myOlePropertyId={unit.property_id}&ProspectId={pid}")
        print(f"\n③ 按 ProspectId 拉 ApplicantInfo：{url[:120]}…")
        try:
            dump("applicantinfo_fragment", session.fetch(url, ajax=True))
        except Exception as exc:  # noqa: BLE001
            print("   拉取失败：", type(exc).__name__, str(exc)[:120])
    else:
        print("\n③ 三份页面里都找不到 ProspectId——无法按 §8.7 的形状拉内容")

    print("\n全程没有提交任何东西。四份 HTML 都在容器的 /tmp/oc_*.html。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="OurCampusBooker 真实单元验证")
    ap.add_argument("--listing", required=True, help="房源 id（带 oc_ 前缀）")
    ap.add_argument("--start", action="store_true",
                    help="真的开始申请（解验证码 + 登录）。不加则只预检，不需要账号")
    ap.add_argument("--diagnose", action="store_true",
                    help="登录后把落地页摊开（不走 booker 的重选兜底、不提交任何东西）")
    ap.add_argument("--even-if-lottery", action="store_true",
                    help="抽签房也走（只跟 --start 一起用）。OC 2026-09 起全站抽签，"
                         "不加这个就验证不到登录——booker 会在登录前停下")
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

    if args.diagnose:
        return _diagnose(listing)
    if args.start:
        return _start(listing, even_if_lottery=args.even_if_lottery)
    if args.even_if_lottery:
        print("--even-if-lottery 只跟 --start 一起用；预检本来就会走到条款页。")
    print(f"预检 {args.listing}（不解验证码、不登录）")
    return _preflight(listing)


if __name__ == "__main__":
    raise SystemExit(main())
