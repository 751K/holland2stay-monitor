"""monitor 启动自检真的会把注册表漂移读出来、报出来、存回去。

单独一个文件，测的是**接线**而不是算法。算法在 tests/test_target_config.py 的
TestRegistryDrift 里，那些用例直接调 ``registry_drift()``；这里调的是 monitor
真正会调的那个函数，用真的 Storage 和真的 meta 表。

分开写是有教训的：2026-09-05 有一条迁移测试拿自己的参数调了辅助函数，于是把生产
那一侧改坏了它照样绿。算法测得再密，接线错了一样什么都不会发生。
"""
from __future__ import annotations

import json
import logging

import pytest

import monitor
from target_config import REGISTRY_SEEN_META_KEY, ack_registry, registry_of


def _snapshot_without(key: str, dropped: str) -> dict[str, list[str]]:
    """一份「上次看到的注册表」，其中 ``key`` 少了 ``dropped`` 那一项。"""
    seen = ack_registry()
    seen[key] = [k for k in seen[key] if k != dropped]
    return seen


class TestCheckRegistryDriftWiring:

    def test_first_run_writes_a_snapshot_and_says_nothing(self, temp_db, caplog):
        assert temp_db.get_meta(REGISTRY_SEEN_META_KEY, "") in ("", "—")
        with caplog.at_level(logging.WARNING, logger="monitor"):
            monitor._check_registry_drift(temp_db)

        assert "配置提示" not in caplog.text
        stored = json.loads(temp_db.get_meta(REGISTRY_SEEN_META_KEY))
        assert stored["PLAZA_CITIES"] == sorted(registry_of("PLAZA_CITIES"))

    def test_a_new_unsubscribed_city_reaches_the_log(
            self, temp_db, caplog, monkeypatch):
        """Rijswijk 那一次的完整复现：注册表有、订阅串没有。"""
        temp_db.set_meta(
            REGISTRY_SEEN_META_KEY,
            json.dumps(_snapshot_without("PLAZA_CITIES", "rijswijk")))
        monkeypatch.setenv("PLAZA_CITIES", "Utrecht,utrecht")

        with caplog.at_level(logging.WARNING, logger="monitor"):
            monitor._check_registry_drift(temp_db)

        assert "PLAZA_CITIES" in caplog.text
        assert "Rijswijk" in caplog.text

    def test_it_says_so_again_after_a_restart(self, temp_db, caplog, monkeypatch):
        """没处理就一直说。报一次就闭嘴的话，重启就等于把问题藏了。"""
        temp_db.set_meta(
            REGISTRY_SEEN_META_KEY,
            json.dumps(_snapshot_without("PLAZA_CITIES", "rijswijk")))
        monkeypatch.setenv("PLAZA_CITIES", "Utrecht,utrecht")

        monitor._check_registry_drift(temp_db)          # 第一次启动
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="monitor"):
            monitor._check_registry_drift(temp_db)      # 第二次启动
        assert "Rijswijk" in caplog.text

    def test_subscribing_the_city_ends_it(self, temp_db, caplog, monkeypatch):
        temp_db.set_meta(
            REGISTRY_SEEN_META_KEY,
            json.dumps(_snapshot_without("PLAZA_CITIES", "rijswijk")))
        monkeypatch.setenv("PLAZA_CITIES", "|".join(
            f"{n},{k}" for k, n in registry_of("PLAZA_CITIES").items()))

        with caplog.at_level(logging.WARNING, logger="monitor"):
            monitor._check_registry_drift(temp_db)
        assert "Rijswijk" not in caplog.text

    def test_a_broken_snapshot_does_not_stop_startup(self, temp_db, caplog):
        """meta 里是坏 JSON 时按「第一次运行」处理，不能抛。

        这只是一条提醒。为了一条提醒让 monitor 起不来，比提醒本身贵得多。
        """
        temp_db.set_meta(REGISTRY_SEEN_META_KEY, "{不是 JSON")
        monitor._check_registry_drift(temp_db)          # 不抛即可
        assert json.loads(temp_db.get_meta(REGISTRY_SEEN_META_KEY))

    def test_bootstrap_actually_calls_it(self):
        """接线本身：_bootstrap_settings 里必须有这一句。

        少了这句，上面每一条都还是绿的，而生产里这个检查一次都不会跑。
        """
        import inspect
        src = inspect.getsource(monitor._bootstrap_settings)
        assert "_check_registry_drift" in src
