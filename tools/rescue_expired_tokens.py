"""rescue_expired_tokens.py — 把刚过期、设备还挂着的 token 续回来
=================================================================
用法::

    docker compose exec -T h2s python3 - < tools/rescue_expired_tokens.py
    docker compose exec -T h2s python3 - --apply < tools/rescue_expired_tokens.py

为什么需要这一次性补救
----------------------
``app_tokens.expires_at`` 原来是登录时写死 now+90d、之后只读。到期那一刻
``get_active_devices_for_user`` 的 JOIN 就把设备滤掉，推送立即停——而推送正是
把用户叫回 App 的唯一通道。拿不到推送的人不会打开 App，也就看不到 401、不会
重新登录。

2026-08-28 实测：48 个登录过 App 的用户里 9 个已经这么掉出去了，**重新登录
回来的 0 人**。其中 Zhou / Xu 在过期前三天还在用 App。

mstorage/_tokens.py 的 touch_app_tokens 已经改成滑动续期，从此不会再有人这么
掉出去。但它救不了已经过期的——那些 token 再也不会被使用，也就再也不会被
touch 到。这个脚本补的就是这一批。

判据
----
同时满足才续：

1. ``revoked = 0``        自己登出 / 被撤销的不救。那是用户的意思，不是 bug。
2. ``expires_at`` 已过期  没过期的交给滑动续期，这里不碰。
3. 过期不超过 ``--within`` 天（默认 30）
       久到不像还在用的不救。一个半年前就断了的 token 续回来，只是凭空延长
       一个早已废弃的会话。
4. 名下至少一台**未禁用**的设备
       没有设备的话续了也不产生任何推送，只是白白延长一个会话的有效期。

这是替用户延长了他自己没有明示同意过的会话，所以判据往窄了取：救的是「本来
就该还活着、只因为一个固定到期日而断掉」的那批。
"""
import sys
from pathlib import Path

# 生产上代码打进镜像，脚本一般是喂 stdin 跑的，那时 __file__ 不存在。
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
except NameError:
    pass

import argparse
from datetime import datetime, timedelta, timezone

from app.services.listing_service import storage_ctx
from mstorage._tokens import SLIDING_TTL_DAYS
from users import load_users


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="实际写入；不加则只报告")
    ap.add_argument("--within", type=int, default=30,
                    help="只救过期不超过这么多天的（默认 30）")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    floor_iso = (now - timedelta(days=args.within)).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_iso = (now + timedelta(days=SLIDING_TTL_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")

    names = {u.id: u.name for u in load_users()}

    with storage_ctx() as st:
        conn = st.conn
        rows = conn.execute(
            """
            SELECT t.id, t.user_id, t.created_at, t.expires_at, t.last_used_at,
                   COUNT(d.id) AS devices
            FROM app_tokens t
            JOIN device_tokens d
              ON d.app_token_id = t.id AND d.disabled_at IS NULL
            WHERE t.revoked = 0
              AND t.expires_at IS NOT NULL
              AND t.expires_at <  ?
              AND t.expires_at >= ?
              AND t.user_id IS NOT NULL
            GROUP BY t.id
            ORDER BY t.expires_at
            """,
            (now_iso, floor_iso),
        ).fetchall()

        if not rows:
            print(f"没有符合条件的 token（过期 ≤ {args.within} 天且仍有设备）。")
            return 0

        print(f"符合条件的 token {len(rows)} 个，涉及 "
              f"{len({r['user_id'] for r in rows})} 个用户：\n")
        for r in rows:
            exp = datetime.fromisoformat(r["expires_at"].replace("Z", "+00:00"))
            used = r["last_used_at"]
            used_days = "?"
            if used:
                u = datetime.fromisoformat(used.replace("Z", "+00:00"))
                used_days = str((now - u).days)
            print(f"  {names.get(r['user_id'], r['user_id'])[:26]:<28}"
                  f"过期 {(now - exp).days:>2} 天   "
                  f"上次使用 {used_days:>3} 天前   "
                  f"设备 {r['devices']}")

        if not args.apply:
            print(f"\n这是预演。加 --apply 会把这 {len(rows)} 个的 expires_at "
                  f"推到 {new_iso}。")
            return 0

        ids = [r["id"] for r in rows]
        ph = ",".join("?" * len(ids))
        with conn:
            cur = conn.execute(
                f"UPDATE app_tokens SET expires_at = ? "
                f"WHERE id IN ({ph}) AND revoked = 0 AND expires_at < ?",
                [new_iso, *ids, new_iso],
            )
        print(f"\n已续期 {cur.rowcount} 个 token，新到期日 {new_iso}。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
