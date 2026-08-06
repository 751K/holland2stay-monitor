"""运维配置从 .env 搬进 SQLite：取值顺序与一次性迁移。

    真实环境变量  >  app_settings 表  >  代码默认值

这套顺序有一个特别容易出的失败形态，而且它**完全静默**：只要 .env 里还留着同名
键，``load_dotenv()`` 就会把它塞进 ``os.environ``，于是它永远赢过数据库——面板保存
成功、提示成功、什么都没发生。因此迁移是「搬」而不是「抄」：写进库之后必须从
.env 移除，**还要从内存里的 os.environ 撤掉**（config 导入时早就 load_dotenv 过
一遍了）。后半句是写这个模块时才被自己的测试逼出来的，漏掉它首次升级后的整程
里面板都是失效的。

本文件因此重点守两件事：迁移之后面板改得动，以及真实环境变量仍然赢得过数据库。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import settings_store as ss
from storage import Storage


@pytest.fixture
def env_and_db(tmp_path, monkeypatch):
    """一份干净的 .env + 空库，且不碰进程真实的 os.environ。"""
    monkeypatch.setattr(ss, "_hydrated", set())
    env = tmp_path / ".env"
    env.write_text(
        "WEB_PASSWORD=secret\n"
        "DB_PATH=data/x.db\n"
        "CHECK_INTERVAL=300\n"
        "PEAK_START=08:30\n"
        "CITIES=Eindhoven,29\n",
        encoding="utf-8",
    )
    st = Storage(tmp_path / "t.db")
    for key in ("CHECK_INTERVAL", "PEAK_START", "CITIES", "MIN_INTERVAL"):
        monkeypatch.delenv(key, raising=False)
    yield env, st
    st.close()


class TestMigration:
    def test_moves_runtime_keys_into_the_table(self, env_and_db):
        env, st = env_and_db
        moved, skipped = ss.migrate_env_to_db(st, env)

        assert moved == ["CHECK_INTERVAL", "CITIES", "PEAK_START"]
        assert skipped == []
        assert st.get_app_setting("CHECK_INTERVAL") == "300"

    def test_removes_them_from_the_file(self, env_and_db):
        """不删就静默失效：.env 会被 load_dotenv 塞进环境，永远赢过数据库。"""
        env, st = env_and_db
        ss.migrate_env_to_db(st, env)

        assert ss.env_overrides(env) == []
        text = env.read_text(encoding="utf-8")
        assert "CHECK_INTERVAL" not in text

    def test_leaves_secrets_and_deploy_keys_alone(self, env_and_db):
        """凭据与 DB_PATH 必须留在文件里——后者是引导顺序的硬约束。"""
        env, st = env_and_db
        ss.migrate_env_to_db(st, env)

        text = env.read_text(encoding="utf-8")
        assert "WEB_PASSWORD=secret" in text
        assert "DB_PATH=data/x.db" in text
        assert st.get_app_setting("WEB_PASSWORD") is None

    def test_backs_up_before_touching_anything(self, env_and_db):
        env, st = env_and_db
        ss.migrate_env_to_db(st, env)

        backups = list(env.parent.glob(".env.bak.*"))
        assert len(backups) == 1
        assert "CHECK_INTERVAL=300" in backups[0].read_text(encoding="utf-8")

    def test_is_idempotent(self, env_and_db):
        env, st = env_and_db
        ss.migrate_env_to_db(st, env)
        assert ss.migrate_env_to_db(st, env) == ([], [])

    def test_database_wins_over_a_stale_env_value(self, env_and_db):
        """库里已有值时不覆盖：重跑迁移不能用陈旧的 .env 盖掉面板刚改的配置。"""
        env, st = env_and_db
        st.set_app_setting("CHECK_INTERVAL", "60", updated_by="panel")

        moved, skipped = ss.migrate_env_to_db(st, env)

        assert "CHECK_INTERVAL" in skipped
        assert st.get_app_setting("CHECK_INTERVAL") == "60"
        assert ss.env_overrides(env) == [], "跳过的键也要删，否则继续盖住数据库"

    def test_records_who_did_it(self, env_and_db):
        env, st = env_and_db
        ss.migrate_env_to_db(st, env)
        assert st.app_settings_meta()["CHECK_INTERVAL"][1] == "migration"

    def test_no_env_file_is_not_an_error(self, tmp_path):
        st = Storage(tmp_path / "t.db")
        try:
            assert ss.migrate_env_to_db(st, tmp_path / "nope.env") == ([], [])
        finally:
            st.close()


class TestResolutionOrder:
    def test_table_value_reaches_the_process(self, env_and_db):
        env, st = env_and_db
        ss.migrate_env_to_db(st, env)
        assert ss.hydrate(st) == 3
        assert os.environ["CHECK_INTERVAL"] == "300"
        assert ss.source_of("CHECK_INTERVAL") == "db"

    def test_real_env_var_wins(self, env_and_db, monkeypatch):
        """容器化排障要能强制覆盖：docker compose run -e CHECK_INTERVAL=30 ...

        注意值必须与 .env 里的不同——相同时分辨不出是文件还是环境变量，
        见 _forget_migrated() 的说明。
        """
        env, st = env_and_db
        monkeypatch.setenv("CHECK_INTERVAL", "30")
        ss.migrate_env_to_db(st, env)
        ss.hydrate(st)

        assert os.environ["CHECK_INTERVAL"] == "30"
        assert ss.source_of("CHECK_INTERVAL") == "env"

    def test_unset_key_falls_through_to_code_default(self, env_and_db):
        env, st = env_and_db
        ss.hydrate(st)
        assert ss.source_of("MIN_INTERVAL") == "default"
        assert "MIN_INTERVAL" not in os.environ

    def test_panel_edit_takes_effect_after_rehydrate(self, env_and_db):
        """整条链最关键的一环：迁移之后面板改得动。

        这里正是最初漏掉 _forget_migrated() 时会挂的地方——os.environ 里还留着
        config 导入时加载的旧值，hydrate 会以为是外部强制指定而跳过。
        """
        env, st = env_and_db
        # 模拟 config 导入时 load_dotenv 的效果
        os.environ["CHECK_INTERVAL"] = "300"
        ss.migrate_env_to_db(st, env)
        ss.hydrate(st)

        st.set_app_setting("CHECK_INTERVAL", "45", updated_by="panel")
        ss.hydrate(st)

        assert os.environ["CHECK_INTERVAL"] == "45", "面板改了不生效"

    def test_hydrate_is_idempotent(self, env_and_db):
        env, st = env_and_db
        ss.migrate_env_to_db(st, env)
        assert ss.hydrate(st) == ss.hydrate(st) == 3

    def test_non_runtime_rows_are_never_injected(self, env_and_db, caplog):
        """表里混进凭据也不能注入环境变量——它会流进子进程和崩溃报告。"""
        env, st = env_and_db
        st.set_app_setting("WEB_PASSWORD", "leaked")
        with caplog.at_level("WARNING"):
            ss.hydrate(st)
        assert os.environ.get("WEB_PASSWORD") != "leaked"
        assert any("WEB_PASSWORD" in r.getMessage() for r in caplog.records)

    def test_storage_failure_falls_back_instead_of_crashing(self, env_and_db):
        """配置读不出来不该让监控起不来——用默认值照常跑。"""
        env, st = env_and_db

        class Broken:
            def all_app_settings(self):
                raise RuntimeError("database is locked")

        assert ss.hydrate(Broken()) == 0


class TestEnvOverrideIsVisible:
    """强制覆盖必须看得见，否则就是一次静默失败。"""

    def test_leftover_runtime_keys_are_reported(self, env_and_db):
        env, st = env_and_db
        assert set(ss.env_overrides(env)) == {"CHECK_INTERVAL", "PEAK_START", "CITIES"}

    def test_comments_and_blank_lines_are_ignored(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("# CHECK_INTERVAL=300\n\nPEAK_START=08:30\n", encoding="utf-8")
        assert ss.env_overrides(env) == ["PEAK_START"]

    def test_monitor_warns_about_leftovers(self):
        import inspect

        import monitor

        src = inspect.getsource(monitor._bootstrap_settings)
        assert "env_overrides" in src, "手工加回 .env 的键会静默盖过面板"


class TestWiredIntoBothProcesses:
    """注水挂在启动路径上，否则它只是一个没人调的纯函数。

    两个进程各有自己的 os.environ，缺哪个哪个就用错配置。
    """

    def test_monitor_hydrates_before_load_config(self):
        import inspect

        import monitor

        src = inspect.getsource(monitor._async_main)
        assert src.index("_bootstrap_settings()") < src.index("cfg = load_config()"), (
            "load_config() 读的就是 os.environ，晚一步就读到默认值了"
        )

    def test_monitor_rehydrates_on_reload(self):
        import inspect

        import monitor

        src = inspect.getsource(monitor.main_loop)
        i = src.index("load_dotenv(dotenv_path=ENV_PATH, override=True)")
        assert "_reload_settings()" in src[i:i + 400], (
            "override=True 重放了 .env，不跟着刷新的话面板刚改的值这一程都不生效"
        )

    def test_web_hydrates_at_import_not_in_main(self):
        """gunicorn 加载的是 web:app，根本不经过 main()。"""
        src = (Path(__file__).resolve().parent.parent / "web.py").read_text(encoding="utf-8")
        assert "_hydrate_settings()" in src
        assert src.index("_hydrate_settings()\n") < src.index("app = Flask(")

    def test_only_monitor_migrates(self):
        """多个 gunicorn worker 并发改写 .env 会打架，迁移必须独占。"""
        src = (Path(__file__).resolve().parent.parent / "web.py").read_text(encoding="utf-8")
        assert "migrate_env_to_db" not in src


class TestPanelWritesToTheTable:
    def test_route_no_longer_writes_env(self):
        """按**调用**判定，不按字符串——注释里还提着 write_env_key 的来历。"""
        import ast

        src = (Path(__file__).resolve().parent.parent
               / "app" / "routes" / "settings.py").read_text(encoding="utf-8")
        called = {
            ast.unparse(n.func)
            for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call)
        }
        assert "write_env_key" not in called, ".env 又多了一个写入者，锁就退不掉了"
        assert any(c.endswith("set_app_settings") for c in called)

    def test_route_asks_monitor_to_reload(self):
        """不触发热重载的话，改完要等到下次重启才生效——人会以为没保存上。"""
        src = (Path(__file__).resolve().parent.parent
               / "app" / "routes" / "settings.py").read_text(encoding="utf-8")
        assert "write_reload_request()" in src

    def test_route_warns_when_env_overrides(self):
        src = (Path(__file__).resolve().parent.parent
               / "app" / "routes" / "settings.py").read_text(encoding="utf-8")
        assert "source_of" in src and "env_overridden" in src
