"""plaza_booker_probe.py — 用真实账号验一次 PlazaBooker 的端到端
==================================================================
**在你自己的终端里跑。密码用 getpass 读，不落盘、不进 shell 历史、不回显。**

    python tools/plaza_booker_probe.py                  # 默认：只预检，不提交
    python tools/plaza_booker_probe.py --listing pz_16626
    python tools/plaza_booker_probe.py --listing pz_XXXX --submit   # 真的应征

为什么需要它
------------
`bookers/plaza.py` 里每一步都单独验过了（登录端点的字段名用形状探针验、下单请求
用抓包验），但**没验过我们的 Python 代码把它们串起来能不能跑通**。串起来才会暴露
的东西：cookie 有没有被 curl_cffi 的 session 正确带上、form 信封取得到取不到、
两步登录的顺序对不对。

默认目标是「你已经应征过的房源」
--------------------------------
那种房源的预检会返回 ``WINKEL-REACTIE-DUBBEL``，于是：

- 登录、portal 会话、getobject、原因码映射 —— 全都真实走了一遍
- **不会产生任何新的应征**，因为预检不过就不发写请求

也就是说默认这条路把「能不能跑通」验掉，同时把「会不会误伤」降到零。想验
``kanReageren=true`` 那条分支就换一个没应征过的 id，仍然默认不提交。

``--submit`` 会真的应征
-----------------------
它要求你再手打一次房源 id 确认。这不是仪式感：预检通过之后那一发就是真实应征，
和你在网站上点 Reply 完全一样。
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


def main() -> int:
    ap = argparse.ArgumentParser(description="PlazaBooker 端到端验证")
    ap.add_argument("--listing", default="pz_16626",
                    help="房源 id（带 pz_ 前缀）。默认是一条你已应征过的，只验通路")
    ap.add_argument("--submit", action="store_true",
                    help="真的提交应征。不加则只走到预检")
    ap.add_argument("-v", "--verbose", action="store_true", help="打开 DEBUG 日志")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s")

    from bookers.base import BookingRequest
    from bookers.plaza import PlazaBooker
    from config import AutoBookConfig
    from models import Listing
    from users import UserConfig

    print("Plaza 账号（用户名不是邮箱）")
    username = input("  用户名: ").strip()
    password = getpass.getpass("  密码（不回显）: ")
    if not (username and password):
        print("没填，退出。")
        return 1

    if args.submit:
        print(f"\n⚠️  --submit 会**真的应征** {args.listing}，和在网站上点 Reply 一样。")
        if input(f"    再打一次房源 id 确认: ").strip() != args.listing:
            print("对不上，取消。")
            return 1

    user = UserConfig(name="probe")
    user.auto_book = AutoBookConfig(plaza_username=username, plaza_password=password)
    listing = Listing(
        id=args.listing, name=f"probe {args.listing}", status="Available to book",
        price_raw="", available_from=None, features=[],
        url="https://plaza.newnewnew.space/", city="", source="plaza")

    print(f"\n跑 PlazaBooker：{args.listing}  dry_run={not args.submit}")
    result = PlazaBooker().book(
        BookingRequest(listing=listing, user=user, dry_run=not args.submit))

    print("\n── 结果 ──────────────────────────────")
    print(f"  success : {result.success}")
    print(f"  phase   : {result.phase}")
    print(f"  dry_run : {result.dry_run}")
    print(f"  message : {result.message}")
    print("──────────────────────────────────────")

    # 怎么读这个结果
    hints = {
        "not_configured": "凭据没进去——脚本自己的问题，不是账号问题。",
        "auth_failed": "登录被拒。要么密码不对，要么 oauth/token 的字段名推错了"
                       "（形状探针验过，但没用真凭据跑过）。",
        "unknown_error": "看 message：如果是「没有 result 键」，那是 Content-Type "
                         "或端点用法的问题；如果是原因码，那是个还没侦察过的码。",
        "success": "通了。登录 → portal 会话 → 预检 全部真实跑过。",
        "dry_run": "通了。预检说这条现在可以应征（没提交）。",
        "race_lost": "通了。这条房源已经不能应征了（不再发布 / 被抢）。",
    }
    if result.phase in hints:
        print(f"\n{hints[result.phase]}")
    return 0 if result.success or result.phase in ("dry_run", "race_lost") else 2


if __name__ == "__main__":
    raise SystemExit(main())
