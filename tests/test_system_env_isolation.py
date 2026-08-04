"""/system 这一页读 .env 不能改进程环境。

背景（2026-08-04 本地走查发现）：``system_info()`` 为了显示 .env 的**当前**内容，
调了 ``load_dotenv(override=True)``。那个调用不是"读"，是"写"——它把 .env 里的
每个键强灌进 os.environ，永久生效：

- docker-compose ``environment:`` 里设的值（NO_PROXY / 代理等）会被 .env 的
  同名键顶掉；
- WEB_PASSWORD 变了鉴权开关当场翻转，FLASK_SECRET 变了所有已登录会话失效。

而这一页每 30 秒整页自动刷新，等于每 30 秒重来一遍。走查时就是这么被踢回
登录页的：进程环境里 WEB_PASSWORD 本来是空的（鉴权关），打开 /system 之后
.env 里的真密码被灌进来，鉴权自己打开了。

这里必须自己造 .env 并改掉 ``app.routes.system.ENV_PATH``——该模块是
``from config import ENV_PATH``，import 时就把常量抓走了，conftest 对
``config.ENV_PATH`` 的 monkeypatch 打不到它。不这么做，测试会去读开发机上
真实的 .env，既不可复现，也根本盖不住这个 bug。
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    """一份含 CHECK_INTERVAL / TIMEZONE 的 .env，并让 /system 真去读它。"""
    from app.routes import system as system_route

    path = tmp_path / "dotenv"
    path.write_text(
        "CHECK_INTERVAL=111\nTIMEZONE=Europe/Amsterdam\nH2S_ONLY_IN_FILE=yes\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(system_route, "ENV_PATH", path)
    return path


class TestSystemPageDoesNotMutateEnv:
    def test_env_var_set_outside_dotenv_survives(self, admin_client, env_file, monkeypatch):
        """进程里设的值不能被 .env 的同名键顶掉。

        这正是 docker-compose ``environment:`` 与 .env 同时给同一个键时的情形。
        """
        monkeypatch.setenv("CHECK_INTERVAL", "999")
        monkeypatch.setenv("TIMEZONE", "UTC")

        assert admin_client.get("/system").status_code == 200

        assert os.environ["CHECK_INTERVAL"] == "999"
        assert os.environ["TIMEZONE"] == "UTC"

    def test_repeated_loads_stay_stable(self, admin_client, env_file, monkeypatch):
        """页面每 30 秒自动刷一次；刷多少次都不该有累积效应。"""
        monkeypatch.setenv("CHECK_INTERVAL", "999")
        for _ in range(3):
            assert admin_client.get("/system").status_code == 200
        assert os.environ["CHECK_INTERVAL"] == "999"

    def test_key_only_in_dotenv_is_not_left_behind(self, admin_client, env_file):
        """.env 里有、进程里原本没有的键，读完也不该留下。"""
        os.environ.pop("H2S_ONLY_IN_FILE", None)
        assert admin_client.get("/system").status_code == 200
        assert "H2S_ONLY_IN_FILE" not in os.environ

    def test_page_still_shows_the_dotenv_value(self, admin_client, env_file, monkeypatch):
        """还原不能把功能一起还原掉——页面显示的仍须是 .env 里的当前值。"""
        monkeypatch.delenv("CHECK_INTERVAL", raising=False)
        body = admin_client.get("/system").get_data(as_text=True)
        assert "111" in body
