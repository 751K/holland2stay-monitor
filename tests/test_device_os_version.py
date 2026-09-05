"""``device_tokens.os_version``：客户端系统版本的上报与存储。

为什么这一列值得一整个测试文件
------------------------------
这件事里绝大多数错法都会**明着报错**——列没建就是 OperationalError，字段没
透传就是 TypeError。只有一处不会：

    UPDATE 分支漏写。

端点的 summary 是 "Register or refresh a push device"，而 iOS 每次启动只要
「已登录 + 非 guest + 有推送权限」就调一次，用的是同一个 device_token。也就是
说**所有现存用户走的都是 UPDATE 分支，不是 INSERT**。只在插入时写 os_version：

- 新装的用户有数据 → 看起来在正常工作
- 老用户永远是 NULL → 而他们正是要统计的那批人
- 用户升级系统之后值不会变 → 冻结在第一次注册时的版本，比没有更糟

三条现象没有一条会抛异常，也没有一条会让别的测试变红。所以这里第一条测的就
是它，而且是「先注册一次，再用不同版本注册一次，断言值变了」——不是「注册一次
断言有值」，后者在只写 INSERT 的实现下同样是绿的。

数据是拿来干什么的
------------------
``model`` × ``os_version`` 的交叉表（限 ``disabled = false``）。``model`` 上报的
已经是硬件标识符（``iPhone16,2`` 这种，取自 ``utsname.machine``），所以「机型
够格跑端上模型 且 系统够新」这个判断，有了这一列就能算。ASC Analytics 给的是
两张互相独立的边缘分布，拼不出交叉表——这是自己收一份的唯一理由。
"""

from __future__ import annotations

import pytest

from config import ListingFilter
from users import UserConfig, save_users, set_app_password


@pytest.fixture
def api_app(test_app, tmp_path, monkeypatch):
    monkeypatch.setattr("app.db.DB_PATH", tmp_path / "dev_os_version.db")
    from app import api_auth
    api_auth.invalidate_token_cache()
    yield test_app
    api_auth.invalidate_token_cache()


@pytest.fixture
def api_client(api_app):
    return api_app.test_client()


@pytest.fixture
def two_users(api_app):
    users = []
    for name, uid in [("kong", "kong0001"), ("alice", "alice002")]:
        u = UserConfig(name=name, id=uid, listing_filter=ListingFilter())
        u.app_login_enabled = True
        set_app_password(u, f"{name}_pw_xyz")
        users.append(u)
    save_users(users)
    return users


def _login(api_client, username, password):
    r = api_client.post("/api/v1/auth/login",
                        json={"username": username, "password": password})
    return r.get_json()["data"]["token"]


def _bearer(tok):
    return {"Authorization": f"Bearer {tok}"}


TOKEN = "a" * 64


def _seed(db, user_id="kong0001"):
    tid, _ = db.create_app_token(role="user", user_id=user_id)
    return tid


def _row(db, tid):
    rows = db.list_devices_for_token(tid)
    assert len(rows) == 1, f"预期只有一行设备，实际 {len(rows)}"
    return rows[0]


class TestUpsertBranches:
    """这一节是整件事唯一「看上去成了其实没成」的地方。"""

    def test_a_second_registration_overwrites_the_recorded_version(self, temp_db):
        """升级系统之后值必须跟着变——只写 INSERT 分支的实现在这里会红。"""
        tid = _seed(temp_db)
        temp_db.register_device(app_token_id=tid, device_token=TOKEN,
                                model="iPhone16,2", os_version="18.5")
        temp_db.register_device(app_token_id=tid, device_token=TOKEN,
                                model="iPhone16,2", os_version="26.0.1")

        assert _row(temp_db, tid)["os_version"] == "26.0.1", (
            "第二次注册（UPDATE 分支）没有写 os_version。iOS 每次启动都用同一个 "
            "device_token 调一次注册，所以所有现存用户走的都是这条分支——"
            "只在 INSERT 里写的话，老用户永远是 NULL，升级系统后值也不会变。")

    def test_an_old_row_that_never_reported_gets_a_value_on_the_next_launch(self, temp_db):
        """存量设备：先前没上报过，客户端升到 2.1.0 之后第一次启动就该有值。"""
        tid = _seed(temp_db)
        temp_db.register_device(app_token_id=tid, device_token=TOKEN)   # 老客户端
        assert _row(temp_db, tid)["os_version"] is None

        temp_db.register_device(app_token_id=tid, device_token=TOKEN,
                                os_version="26.0")                       # 升级后
        assert _row(temp_db, tid)["os_version"] == "26.0", (
            "存量行在 UPDATE 分支上没有被补上值——而存量行正是绝大多数。")

    def test_the_first_registration_records_it_too(self, temp_db):
        """INSERT 分支（新装用户）。放在 UPDATE 之后，因为它才是少数情况。"""
        tid = _seed(temp_db)
        temp_db.register_device(app_token_id=tid, device_token=TOKEN,
                                os_version="18.5")
        assert _row(temp_db, tid)["os_version"] == "18.5"


class TestMissingIsDistinguishable:
    """NULL 必须能和「报上来了但值奇怪」分开，否则交叉表的分母是错的。"""

    def test_a_client_that_does_not_send_it_leaves_null_not_empty_string(self, temp_db):
        tid = _seed(temp_db)
        temp_db.register_device(app_token_id=tid, device_token=TOKEN)
        assert _row(temp_db, tid)["os_version"] is None, (
            "没上报时存成了空串。'' 会把「不知道」伪装成「上报了一个空值」，"
            "统计时这两者必须分开。")

    def test_an_empty_string_is_also_stored_as_null(self, temp_db):
        """客户端发 \"\" 和不发是同一件事，不该在库里长成两种样子。"""
        tid = _seed(temp_db)
        temp_db.register_device(app_token_id=tid, device_token=TOKEN, os_version="   ")
        assert _row(temp_db, tid)["os_version"] is None

    def test_the_column_has_no_default(self, temp_db):
        """迁移用的是 ``TEXT``，不是 ``TEXT NOT NULL DEFAULT ''``。"""
        cols = {r["name"]: r for r in temp_db.conn.execute(
            "PRAGMA table_info(device_tokens)").fetchall()}
        assert "os_version" in cols, "device_tokens 上没有 os_version 列"
        col = cols["os_version"]
        assert not col["notnull"], "os_version 被建成了 NOT NULL——老行没法表达「未上报」"
        assert col["dflt_value"] is None, (
            f"os_version 有默认值 {col['dflt_value']!r}——默认值会把「未上报」变成一个真值")


class TestNoShapeValidation:
    """别卡正则：被丢掉的恰好是还没见过的那些系统版本。"""

    @pytest.mark.parametrize("value", [
        "18.5",        # 常见
        "26.0",        # 两段
        "18.5.1",      # 三段
        "26.1 beta 2", # Apple 偶尔给带空格的
        "15",          # 只有一段
        "Android 16",  # 将来 Android 报的形状完全不同
    ])
    def test_unusual_shapes_are_stored_verbatim(self, temp_db, value):
        tid = _seed(temp_db)
        temp_db.register_device(app_token_id=tid, device_token=TOKEN, os_version=value)
        assert _row(temp_db, tid)["os_version"] == value, (
            f"{value!r} 没有被原样存下来。做形状校验的话，被丢掉的恰好是还没"
            "见过的那些版本——而那正是想了解的。")

    def test_an_overlong_value_is_truncated_not_rejected(self, temp_db):
        """32 字符是列宽约定；超了就截，不能因此让整台设备注册失败。"""
        tid = _seed(temp_db)
        temp_db.register_device(app_token_id=tid, device_token=TOKEN,
                                os_version="x" * 200)
        stored = _row(temp_db, tid)["os_version"]
        assert stored == "x" * 32, f"截断长度不对：{len(stored or '')}"


class TestEndpoint:
    """走完整的 HTTP 路径——路由层读 ``body.get("os_version")`` 那一步只有这里覆盖。"""

    def test_a_request_without_os_version_still_registers(self, api_client, two_users):
        """低于 2.1.0 的 iOS 和当前的 Android 都不发它。不能设成必填。"""
        tok = _login(api_client, "kong", "kong_pw_xyz")
        r = api_client.post("/api/v1/devices/register",
                            json={"device_token": TOKEN, "model": "iPhone16,2"},
                            headers=_bearer(tok))
        assert r.status_code == 200, (
            f"少发 os_version 就被拒了（{r.status_code}）。这会把所有低于 2.1.0 的"
            "客户端挡在设备注册之外——它们连推送都收不到了。")
        item = api_client.get("/api/v1/devices", headers=_bearer(tok)) \
            .get_json()["data"]["items"][0]
        assert item["os_version"] is None

    def test_the_route_reads_and_stores_it(self, api_client, two_users):
        tok = _login(api_client, "kong", "kong_pw_xyz")
        api_client.post("/api/v1/devices/register",
                        json={"device_token": TOKEN, "os_version": "26.0.1"},
                        headers=_bearer(tok))
        item = api_client.get("/api/v1/devices", headers=_bearer(tok)) \
            .get_json()["data"]["items"][0]
        assert item["os_version"] == "26.0.1", (
            "路由层没把 os_version 透传下去——iOS 发了，后端丢了。")

    def test_a_relaunch_updates_it_over_http(self, api_client, two_users):
        """和 TestUpsertBranches 第一条同一件事，但走真实请求路径。"""
        tok = _login(api_client, "kong", "kong_pw_xyz")
        for version in ("18.5", "26.0.1"):
            api_client.post("/api/v1/devices/register",
                            json={"device_token": TOKEN, "os_version": version},
                            headers=_bearer(tok))
        items = api_client.get("/api/v1/devices", headers=_bearer(tok)) \
            .get_json()["data"]["items"]
        assert len(items) == 1, "同一个 device_token 注册两次不该长出两行"
        assert items[0]["os_version"] == "26.0.1"


class TestMigrationOnAnExistingDatabase:
    """存量库走的是 ALTER TABLE，和新库的 CREATE TABLE 是两条不同的代码路径。

    生产库里没有一行是 CREATE TABLE 建出来的——那条分支只在全新部署时走。真正
    要跑的是 ``_add_column_if_missing``，而它的失败方式和建表不一样：列加错了
    类型、或者存量行被写上了默认值，两种都不会抛异常。
    """

    def _drop_the_column(self, db):
        """把库退回「还没有这一列」的样子。"""
        db.conn.execute("ALTER TABLE device_tokens DROP COLUMN os_version")
        db.conn.commit()
        cols = {r["name"] for r in db.conn.execute(
            "PRAGMA table_info(device_tokens)").fetchall()}
        assert "os_version" not in cols, "没能把列删掉，这条测试的前提不成立"

    def _rerun_migrations(self, db):
        """跑**生产那条**迁移，而不是自己拿参数调 ``_add_column_if_missing``。

        第一版就是后者，于是把 ``_base.py`` 里的声明改成
        ``TEXT NOT NULL DEFAULT ''`` 之后测试照样是绿的——它验的是那个 helper
        会不会干活，不是生产传了什么。列的形状恰恰全在那个参数里。
        """
        db._migrate()

    def test_the_column_is_added_and_existing_rows_stay_null(self, temp_db):
        tid = _seed(temp_db)
        self._drop_the_column(temp_db)
        temp_db.conn.execute(
            "INSERT INTO device_tokens (app_token_id, device_token, created_at, last_seen)"
            " VALUES (?, ?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
            (tid, TOKEN))
        temp_db.conn.commit()

        self._rerun_migrations(temp_db)

        row = _row(temp_db, tid)
        assert row["os_version"] is None, (
            "迁移给存量行填了值。存量行的真实情况就是「没上报过」，"
            "填任何东西都是编造。")

    def test_the_migrated_column_is_nullable_with_no_default(self, temp_db):
        self._drop_the_column(temp_db)
        self._rerun_migrations(temp_db)
        col = {r["name"]: r for r in temp_db.conn.execute(
            "PRAGMA table_info(device_tokens)").fetchall()}["os_version"]
        assert not col["notnull"] and col["dflt_value"] is None, (
            f"迁移出来的列形状不对：notnull={col['notnull']} default={col['dflt_value']!r}")

    def test_running_it_twice_is_a_no_op(self, temp_db):
        """每次开库都会调一遍——第二次必须什么都不做。"""
        tid = _seed(temp_db)
        temp_db.register_device(app_token_id=tid, device_token=TOKEN, os_version="26.0")
        self._rerun_migrations(temp_db)
        assert _row(temp_db, tid)["os_version"] == "26.0", "重跑迁移把已有的值冲掉了"


class TestNamedForAllPlatforms:
    """字段名是 os_version 不是 ios_version——这个端点 platform 枚举含 android。"""

    def test_the_column_is_not_named_ios_version(self, temp_db):
        cols = {r["name"] for r in temp_db.conn.execute(
            "PRAGMA table_info(device_tokens)").fetchall()}
        assert "ios_version" not in cols, (
            "设备表上出现了 ios_version。这个端点是跨平台的（platform 枚举是 "
            "ios | android），Android 将来报的是 Android 版本号。崩溃上报那条"
            "路径继续叫 ios_version 是有意的——那个端点只有 iOS 在打。")
