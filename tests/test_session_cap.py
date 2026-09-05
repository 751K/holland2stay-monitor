"""同一 (user_id, device_name) 下的活跃会话上限。

为什么要有上限
--------------
每次登录签一枚新 token，而**从来没有人撤销旧的**。客户端重装、重新登录、每一次
UI 测试启动，各留下一枚 90 天的活跃会话。2026-09-06 查线上：

    Test 账号        791 枚活跃会话
    全站             991 枚          ← Test 占 88%
    9-04 一天        +637 枚         ← 一轮截图测试

用户管理页的卡片按 device_name 归并之后仍然是八个标签、每个后面挂着 ×271。而
归并这件事本身就是上一次为同一个问题做的缓解（模板里那句「线上有人在同一台
vivo 上攒了 7 枚」），637 把缓解也撑爆了。

这批数据没有任何一处会报错：查询照常、推送照常、页面照常渲染。它只是越长越大。

阈值为什么是 10
---------------
改之前量的全站分布：活跃会话超过 5 枚的 (user, device_name) 组一共 6 个，5 个是
Test，第 6 个是某人同一台 vivo 上的 7 枚。留 10 → 真实用户一枚都不撤，Test 从
791 收到 50 上下。数字是按真实分布定的。

为什么按 (user_id, device_name) 分组
------------------------------------
同名再次登录几乎一定是同一台设备又登了一次，旧那枚已经没有客户端在用。按用户
整体设限则会在「一个人有 12 台设备」时误伤；按设备名分组时那是 12 个组，各留
10 枚，谁都不受影响。
"""

from __future__ import annotations

import pytest

from mstorage._tokens import (
    MAX_SESSIONS_PER_DEVICE_NAME,
    _collapse_client_names,
    _device_family,
)


def _active(db, user_id: str, device_name: str) -> int:
    return db.conn.execute(
        "SELECT COUNT(*) FROM app_tokens WHERE user_id=? AND device_name=? AND revoked=0",
        (user_id, device_name),
    ).fetchone()[0]


class TestCapOnIssue:
    def test_the_oldest_sessions_are_revoked_past_the_cap(self, temp_db):
        for _ in range(MAX_SESSIONS_PER_DEVICE_NAME + 15):
            temp_db.create_app_token(role="user", user_id="u1", device_name="iPhone")
        assert _active(temp_db, "u1", "iPhone") == MAX_SESSIONS_PER_DEVICE_NAME, (
            "签发新会话时没有收口。每次登录都签一枚而从不撤销旧的，"
            "一轮 UI 测试就能攒出几百枚——线上 Test 账号曾占全站会话的 88%。")

    def test_the_newest_session_always_survives(self, temp_db):
        """刚签出去的那枚绝不能被自己触发的清理误撤——那等于登录即失效。"""
        last_id = 0
        for _ in range(MAX_SESSIONS_PER_DEVICE_NAME + 5):
            last_id, _tok = temp_db.create_app_token(
                role="user", user_id="u1", device_name="iPhone")
        row = temp_db.conn.execute(
            "SELECT revoked FROM app_tokens WHERE id=?", (last_id,)).fetchone()
        assert row["revoked"] == 0, "最新签发的那枚被撤销了——用户会「登录成功但立刻失效」"

    def test_it_keeps_the_newest_not_an_arbitrary_ten(self, temp_db):
        ids = [temp_db.create_app_token(role="user", user_id="u1",
                                        device_name="iPhone")[0]
               for _ in range(MAX_SESSIONS_PER_DEVICE_NAME + 8)]
        alive = {r["id"] for r in temp_db.conn.execute(
            "SELECT id FROM app_tokens WHERE user_id='u1' AND revoked=0")}
        assert alive == set(ids[-MAX_SESSIONS_PER_DEVICE_NAME:]), (
            "留下的不是最新的那批。撤旧留新是这条规则唯一说得通的方向——"
            "留旧的等于把用户手上正在用的会话踢掉。")

    def test_different_device_names_are_counted_separately(self, temp_db):
        """一个人有 12 台设备时不能被误伤：那是 12 个组，不是一组 12 枚。"""
        for i in range(12):
            temp_db.create_app_token(role="user", user_id="u1",
                                     device_name=f"Device {i}")
        total = temp_db.conn.execute(
            "SELECT COUNT(*) FROM app_tokens WHERE user_id='u1' AND revoked=0"
        ).fetchone()[0]
        assert total == 12, (
            f"12 台不同名的设备被砍到 {total} 台。上限是按 (用户, 设备名) 分组的，"
            "按用户整体设限会把多设备用户踢下线。")

    def test_different_users_are_counted_separately(self, temp_db):
        for uid in ("u1", "u2"):
            for _ in range(MAX_SESSIONS_PER_DEVICE_NAME):
                temp_db.create_app_token(role="user", user_id=uid, device_name="iPhone")
        assert _active(temp_db, "u1", "iPhone") == MAX_SESSIONS_PER_DEVICE_NAME
        assert _active(temp_db, "u2", "iPhone") == MAX_SESSIONS_PER_DEVICE_NAME

    def test_admin_tokens_are_untouched(self, temp_db):
        """admin token 没有 user_id，全都会落进同一组——规则必须跳过它们。

        ⚠️ 这条测试有两道护栏在同时保护它，只有一道是显式的：

        1. ``_retire_surplus_sessions`` 开头的 ``role != "user"`` 判断；
        2. SQL 里 ``user_id = ?`` 传 NULL 时**永远不匹配**。

        单独去掉第 1 条，第 2 条会兜住，这条测试照样是绿的（变异验证过）。它只
        在两条同时失效时才红——比如有人把查询改成 ``user_id IS ?`` 去"修"
        NULL 匹配不上的问题。那正是最可能发生的那种改法，所以这条测试仍然值得
        留着，但别把它当成第 1 条判断的唯一背书。
        """
        for _ in range(MAX_SESSIONS_PER_DEVICE_NAME + 5):
            temp_db.create_app_token(role="admin", user_id=None, device_name="")
        n = temp_db.conn.execute(
            "SELECT COUNT(*) FROM app_tokens WHERE role='admin' AND revoked=0"
        ).fetchone()[0]
        assert n == MAX_SESSIONS_PER_DEVICE_NAME + 5, (
            f"admin 的会话被撤掉了（还剩 {n}）。它们 user_id 全是 NULL、"
            "device_name 也常常是空串，一收就是全撤——管理员会被锁在外面。")


class TestFlattenExistingRows:
    """签发时收口管不到已经躺在库里的那 700 多枚。"""

    def _seed_without_cap(self, db, user_id, device_name, n):
        """绕过 create_app_token 直接插，复现上限存在之前的库。"""
        import hashlib
        with db.conn:
            for i in range(n):
                db.conn.execute(
                    "INSERT INTO app_tokens (token_hash, role, user_id, device_name,"
                    " created_at, expires_at) VALUES (?,?,?,?,?,?)",
                    (hashlib.sha256(f"{user_id}{device_name}{i}".encode()).hexdigest(),
                     "user", user_id, device_name,
                     "2026-01-01T00:00:00Z", "2099-01-01T00:00:00Z"))

    def test_it_flattens_history(self, temp_db):
        self._seed_without_cap(temp_db, "u1", "iPhone 17 Pro Max", 271)
        assert _active(temp_db, "u1", "iPhone 17 Pro Max") == 271

        n = temp_db.retire_surplus_sessions_globally()

        assert n == 271 - MAX_SESSIONS_PER_DEVICE_NAME
        assert _active(temp_db, "u1", "iPhone 17 Pro Max") == MAX_SESSIONS_PER_DEVICE_NAME

    def test_it_is_idempotent(self, temp_db):
        """放在每次启动都跑的位置上，第二次必须是零更新。"""
        self._seed_without_cap(temp_db, "u1", "iPhone", 50)
        first = temp_db.retire_surplus_sessions_globally()
        second = temp_db.retire_surplus_sessions_globally()
        assert first == 40 and second == 0, (
            f"重复执行不是幂等的（第二次又撤了 {second} 枚）——它每次启动都会跑。")

    def test_it_leaves_small_groups_alone(self, temp_db):
        self._seed_without_cap(temp_db, "u1", "vivo V2458A", 7)
        assert temp_db.retire_surplus_sessions_globally() == 0
        assert _active(temp_db, "u1", "vivo V2458A") == 7, (
            "把没超限的组也动了。线上真有人在同一台 vivo 上攒了 7 枚，"
            "阈值 10 就是为了不碰他。")


class TestCardCollapsing:
    """名字种类过多时按机型族归并——面板卡片用。"""

    TEST_ACCOUNT = [
        {"name": "iPhone 17 Pro Max", "sessions": 271, "last_used_at": "2026-09-05"},
        {"name": "iPad Pro 13-inch (M5) (16GB)", "sessions": 241, "last_used_at": "2026-09-05"},
        {"name": "Clone 1 of iPad Pro 13-inch (M5)", "sessions": 151, "last_used_at": "2026-09-04"},
        {"name": "Clone 1 of iPhone 17 Pro", "sessions": 85, "last_used_at": "2026-09-04"},
        {"name": "Clone 1 of iPhone 17 Pro Max", "sessions": 40, "last_used_at": "2026-09-04"},
        {"name": "probe", "sessions": 1, "last_used_at": "2026-09-03"},
        {"name": "meizu MEIZU 18", "sessions": 1, "last_used_at": "2026-05-28"},
        {"name": "iPhone 17 Pro", "sessions": 1, "last_used_at": "2026-05-23"},
    ]

    def test_the_test_account_card_becomes_readable(self):
        out = _collapse_client_names(self.TEST_ACCOUNT)
        assert [d["name"] for d in out] == ["iPhone", "iPad", "其他"]

    def test_no_session_is_lost_in_the_merge(self):
        """会话数必须是族内求和。看这张卡片的目的就是看会话数。"""
        before = sum(d["sessions"] for d in self.TEST_ACCOUNT)
        after = sum(d["sessions"] for d in _collapse_client_names(self.TEST_ACCOUNT))
        assert before == after == 791, f"归并前 {before} 归并后 {after}，数丢了"

    def test_last_used_is_the_max_not_the_first(self):
        out = {d["name"]: d for d in _collapse_client_names(self.TEST_ACCOUNT)}
        assert out["iPad"]["last_used_at"] == "2026-09-05", (
            "取的不是族内最大值。取第一条的话卡片会显示一个偏旧的时间，"
            "看上去像这批设备已经不活跃了。")

    def test_unrecognized_names_are_kept_not_dropped(self):
        """安卓和手填的名字不能被并进 iPhone/iPad，也不能被丢掉。

        卡片上少一台设备，比多八个标签更难发现。
        """
        out = {d["name"]: d for d in _collapse_client_names(self.TEST_ACCOUNT)}
        assert "其他" in out and out["其他"]["sessions"] == 2, (
            "meizu 和 probe 没有被算进「其他」——它们要么被丢了，"
            "要么被错误地并进了某个 Apple 机型族。")

    def test_a_normal_user_card_is_untouched(self):
        """真实用户全站最多 3 种名字，阈值 4 对他们是彻底的无操作。"""
        real = [
            {"name": "iPhone", "sessions": 2, "last_used_at": "2026-09-01"},
            {"name": "vivo V2458A", "sessions": 7, "last_used_at": "2026-09-02"},
            {"name": "iPad", "sessions": 1, "last_used_at": "2026-08-01"},
        ]
        assert _collapse_client_names(real) == real, (
            "普通用户的卡片被归并了。他们的设备名是有信息量的，"
            "并成「iPhone」反而看不出是哪台。")

    @pytest.mark.parametrize("name,family", [
        ("iPhone 17 Pro Max", "iPhone"),
        ("Clone 1 of iPhone 17 Pro", "iPhone"),
        ("iPad Pro 13-inch (M5) (16GB)", "iPad"),
        ("Clone 1 of iPad Pro 13-inch (M5)", "iPad"),
        ("meizu MEIZU 18", "其他"),
        ("vivo V2458A", "其他"),
        ("probe", "其他"),
        ("未命名设备", "其他"),
    ])
    def test_family_detection(self, name, family):
        assert _device_family(name) == family


class TestNoAccountNameIsHardcoded:
    """规则不认账号名——写死的话半年后换个测试账号就失效，而没人会记得改。"""

    def test_the_source_does_not_mention_the_test_account(self):
        from pathlib import Path

        src = Path(__file__).resolve().parents[1] / "mstorage" / "_tokens.py"
        text = src.read_text(encoding="utf-8")
        code = "\n".join(
            line for line in text.splitlines()
            if not line.lstrip().startswith("#")
        )
        for needle in ('"Test"', "'Test'", "f1e17cff"):
            assert needle not in code, (
                f"_tokens.py 的代码里出现了 {needle}。这条规则的触发条件必须是"
                "「会话/名字太多」这种一般性质，不是某个账号的名字或 id。")
