"""
用户配置写入 → monitor 快照失效。

monitor 的 ``user_notifiers`` 是 ``list[(UserConfig, Notifier)]``——整个
UserConfig 在 ``_build_user_notifiers`` 时被快照下来，只在启动和热重载时
重建。任何一次用户配置写入都让那份快照过期，必须写热重载请求。

2026-08-07 线上事故：全仓库 12 处写用户配置的地方只有 ``api_v1/me.py``
一处发了这个请求。用户在面板把收件邮箱从 A 改成 B 之后——

- monitor 拿着旧快照，新房源通知继续发 **A**；
- 面板「发送测试通知」是现场 new 一个 notifier，发 **B**。

两个邮箱同时在收，直到别处偶然触发一次重载。Resend 投递记录里能看到
2026-08-04 21:52 的一次心跳同时发给了一个已经不存在于任何 user_config
的地址。

这里守两条线：
1. 行为——``update_users`` 提交成功后一定发请求，且发的时候库里已经是新值；
   mutator 抛异常回滚时一定不发。
2. 结构——``app/routes/users.py`` 里绕过 ``update_users`` 直接改表的路由
   （优先级调整）必须自己发。这条是 AST 检查，新增同类路由时会失败。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_ROUTES = _ROOT / "app" / "routes" / "users.py"


# ── 行为 ────────────────────────────────────────────────────────────

@pytest.fixture
def spy_reload(monkeypatch):
    """记下每次热重载请求，以及请求发生时数据库里的 email_to。

    记录「当时的库值」是这个 fixture 的重点：请求必须在 commit 之后发，
    否则 monitor 收到通知跑去读库，读到的还是旧值——bug 从「不刷新」变成
    「刷新了个寂寞」，而单纯断言「发了请求」的测试对此完全无感。
    """
    import users

    calls: list[list[str]] = []

    def _fake():
        calls.append([u.email_to for u in users.load_users()])

    monkeypatch.setattr(users, "_invalidate_monitor_snapshot", _fake)
    return calls


def _add_user(name: str = "A", email: str = "old@example.com"):
    import users

    user = users.UserConfig(name=name, email_to=email, email_mode="shared")

    def _append(lst):
        lst.append(user)
        return user

    return users.update_users(_append)


class TestUpdateUsersInvalidates:
    def test_write_requests_reload(self, isolated_data_dir, spy_reload):
        _add_user()
        assert len(spy_reload) == 1

    def test_email_change_requests_reload(self, isolated_data_dir, spy_reload):
        import users
        user = _add_user(email="a@example.com")
        spy_reload.clear()

        def _rename(lst):
            users.get_user(lst, user.id).email_to = "b@example.com"

        users.update_users(_rename)
        assert len(spy_reload) == 1

    def test_reload_sees_committed_value(self, isolated_data_dir, spy_reload):
        """请求必须在 commit 之后发——否则 monitor 读回来的还是旧邮箱。"""
        import users
        user = _add_user(email="a@example.com")
        spy_reload.clear()

        def _rename(lst):
            users.get_user(lst, user.id).email_to = "b@example.com"

        users.update_users(_rename)
        assert spy_reload == [["b@example.com"]]

    def test_delete_requests_reload(self, isolated_data_dir, spy_reload):
        """被删掉的用户如果还留在快照里，会继续收通知。"""
        import users
        user = _add_user()
        spy_reload.clear()

        def _delete(lst):
            lst[:] = [u for u in lst if u.id != user.id]

        users.update_users(_delete)
        assert spy_reload == [[]]

    def test_rollback_does_not_request_reload(self, isolated_data_dir, spy_reload):
        """mutator 抛异常 → 事务回滚 → 没有任何东西变过，不该惊动 monitor。"""
        import users
        _add_user()
        spy_reload.clear()

        def _boom(lst):
            raise ValueError("nope")

        with pytest.raises(ValueError):
            users.update_users(_boom)
        assert spy_reload == []


class TestInvalidateIsNonFatal:
    def test_write_failure_does_not_break_the_write(self, isolated_data_dir, monkeypatch, caplog):
        """重载请求写不下去时，用户数据已经提交了，不能让调用方看到异常。"""
        import users

        def _explode():
            raise OSError("read-only fs")

        monkeypatch.setattr("app.process_ctrl.write_reload_request", _explode)
        with caplog.at_level("WARNING"):
            user = _add_user(email="kept@example.com")

        assert [u.email_to for u in users.load_users()] == ["kept@example.com"]
        assert user.id
        assert any("热重载" in r.message for r in caplog.records)


# ── 结构 ────────────────────────────────────────────────────────────

def _function_defs(path: Path) -> dict[str, ast.FunctionDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        n.name: n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _calls_in(fn: ast.AST) -> set[str]:
    """函数体内所有被调用的名字，包含 ``obj.method`` 的 method 名。"""
    out: set[str] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Name):
            out.add(f.id)
        elif isinstance(f, ast.Attribute):
            out.add(f.attr)
    return out


# 绕过 users.update_users、直接改 user_configs 表的 Storage 方法。
# 新增同类方法时把名字加进来。
_DIRECT_WRITE_METHODS = {"reorder_user", "reorder_users_bulk"}


class TestDirectWriteRoutesRequestReload:
    def test_reorder_routes_request_reload(self):
        """直接改表的路由不走 update_users，必须自己发热重载请求。"""
        offenders = []
        for name, fn in _function_defs(_ROUTES).items():
            calls = _calls_in(fn)
            if not (calls & _DIRECT_WRITE_METHODS):
                continue
            if "_request_monitor_reload" not in calls:
                offenders.append(name)
        assert not offenders, (
            "这些路由直接改了 user_configs 表却没请求热重载，"
            f"monitor 会继续用旧快照: {offenders}"
        )

    def test_guard_is_wired_to_something(self):
        """守卫本身别退化成空断言——至少得真的匹配到路由。"""
        matched = [
            name for name, fn in _function_defs(_ROUTES).items()
            if _calls_in(fn) & _DIRECT_WRITE_METHODS
        ]
        assert len(matched) >= 2, f"没找到直接改表的路由，守卫已失效: {matched}"

    def test_update_users_paths_do_not_double_fire(self):
        """走 update_users 的路由不该再自己发一次——重复请求只是噪音。"""
        doubles = []
        for name, fn in _function_defs(_ROUTES).items():
            calls = _calls_in(fn)
            if "update_users" in calls and "_request_monitor_reload" in calls:
                doubles.append(name)
        assert not doubles, f"update_users 已经发过了，这些路由重复发送: {doubles}"
