"""backfill_push_optin.py — 给已有活跃设备的用户补上通知开关
=============================================================
用法::

    python tools/backfill_push_optin.py            # 只报告，不写（默认）
    python tools/backfill_push_optin.py --apply    # 实际写入

为什么需要这一次性回填
----------------------
``notifications_enabled`` 以前只管外部渠道（邮件 / Telegram / WhatsApp /
iMessage），推送从旁边绕了过去——``push.dispatch`` 只查节流和有没有设备。
现在推送开始遵守这个开关（mcore/push.py:_user_wants_push）。

注册接口把这个开关硬编码成 ``False``（fail-closed），而 App 没有任何端点能
改它。所以「登记设备即视为同意」被加进了 device_service，新用户和之后每次
打开 App 的老用户都会自动翻上来。

问题出在**不打开 App 的人身上**：2026-08-28 线上 33 个活跃设备里，13 个
``last_seen`` 已经超过 30 天。这批人不跑回填就会静默失去推送——他们没做过
任何表示「关闭」的操作，却在一次部署之后不再收到房源。

回填的判据
----------
只看「有没有活跃设备」，不看筛选条件、不看外部渠道、不看最后活跃时间：

- 有活跃设备 = 这个人此刻正在收推送 → 补 True，行为不变
- 没有活跃设备 = 这个人此刻收不到推送 → 不动，行为同样不变

净效果是**没有任何人的收发状态发生变化**。这正是回填该有的样子：它补的是
数据与新语义之间的差，不是趁机改产品行为。

「活跃」直接调 storage.get_active_devices_for_user，不另写 SQL。第一版是照着
它的条件抄了一遍 WHERE，抄漏了 ``expires_at`` ——于是「回填的人」会比「实际
能收到推送的人」多出一批 token 已过期的。判据必须是同一段代码，不是同一段
描述；61 个用户查 61 次的代价，远小于两处条件慢慢分叉。
"""
import sys
from pathlib import Path

# 生产上代码是打进镜像的（docker-compose 只 bind mount data/logs/.env），
# 所以这个脚本通常是喂 stdin 跑的：
#     docker compose exec -T h2s python3 - < tools/backfill_push_optin.py
# 那种情况下 __file__ 不存在，而容器的 WORKDIR 已经是 /app，sys.path 本来
# 就对。只有在仓库里直接执行时才需要补路径。
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
except NameError:
    pass

import argparse

from app.services.listing_service import storage_ctx
from users import load_users, update_users


def _users_with_active_devices(user_ids) -> set[str]:
    """这些 user_id 里哪些有活跃设备。"""
    out: set[str] = set()
    with storage_ctx() as st:
        for uid in user_ids:
            try:
                if st.get_active_devices_for_user(uid):
                    out.add(uid)
            except Exception:
                # 单个用户查失败不该让整次回填中断；它会落到「无设备」一侧，
                # 也就是不动——回填的失败模式必须是「少改」而不是「乱改」。
                pass
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="实际写入；不加则只报告")
    args = ap.parse_args()

    users = [u for u in load_users() if u.name != "__admin__"]
    with_dev = _users_with_active_devices([u.id for u in users])

    targets = [u for u in users
               if u.id in with_dev and not u.notifications_enabled]
    already = [u for u in users if u.id in with_dev and u.notifications_enabled]
    no_dev = [u for u in users if u.id not in with_dev]

    print(f"用户总数（不含 admin）        {len(users)}")
    print(f"有活跃设备                    {len(with_dev)}")
    print(f"  ├─ 开关已经是 True          {len(already)}   不动")
    print(f"  └─ 开关是 False，需要回填    {len(targets)}")
    print(f"无活跃设备                    {len(no_dev)}   不动（本来就收不到推送）")

    if targets:
        print()
        print("将被回填的用户:")
        for u in targets:
            ch = ",".join(u.notification_channels) or "(无外部渠道)"
            print(f"  {u.name[:34]:<36} {ch}")

    if not targets:
        print("\n没有需要回填的用户。")
        return 0

    if not args.apply:
        print(f"\n这是预演。加 --apply 才会写入这 {len(targets)} 条。")
        return 0

    ids = {u.id for u in targets}

    def _flip(all_users):
        n = 0
        for u in all_users:
            if u.id in ids and not u.notifications_enabled:
                u.notifications_enabled = True
                n += 1
        return n

    changed = update_users(_flip)
    print(f"\n已写入 {changed} 条。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
