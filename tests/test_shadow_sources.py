"""影子 source 测试。

影子 source = 照常抓取入库，但**不发任何通知**。用于新平台上线前的静默验证：
先确认它抓得对、数据长什么样，再决定是否对用户开放。

被拦掉的只有「告诉谁」这一步——`storage.diff()` 照常执行，房源进库、状态变更
进流水、参与 stale 收敛和面板统计。
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from config import AvailabilityFilter, CityFilter, Config, load_config
from models import Listing
from monitor import _drop_shadow_sources, run_once
from notifier import BaseNotifier
from storage import Storage


def _listing(lid: str, *, source: str, status: str = "Available to book") -> Listing:
    return Listing(
        id=lid, name=f"L {lid}", status=status, price_raw="€700",
        available_from="2030-01-01", features=[], url=f"https://e.test/{lid}",
        city="C", source=source,
    )


class _Cfg:
    def __init__(self, shadow):
        self.shadow_sources = shadow


# ── 纯函数 ──────────────────────────────────────────────────────────

class TestDropShadowSources:
    def test_no_shadow_is_passthrough(self):
        new = [_listing("a", source="ourcampus")]
        sc = [(_listing("b", source="ourcampus"), "Occupied", "Available to book")]
        kn, ks = _drop_shadow_sources(_Cfg([]), new, sc)
        assert kn is new and ks is sc, "未配置影子时应零开销直接返回原对象"

    def test_filters_only_shadow_source(self):
        new = [_listing("a", source="ourcampus"), _listing("b", source="holland2stay")]
        sc = [
            (_listing("c", source="ourcampus"), "Occupied", "Available to book"),
            (_listing("d", source="ourdomain"), "Occupied", "Available to book"),
        ]
        kn, ks = _drop_shadow_sources(_Cfg(["ourcampus"]), new, sc)
        assert [l.id for l in kn] == ["b"]
        assert [t[0].id for t in ks] == ["d"]

    def test_source_match_is_case_insensitive(self):
        new = [_listing("a", source="OurCampus")]
        kn, _ = _drop_shadow_sources(_Cfg(["ourcampus"]), new, [])
        assert kn == []

    def test_multiple_shadow_sources(self):
        new = [_listing(s, source=s) for s in ("ourcampus", "xior", "holland2stay")]
        kn, _ = _drop_shadow_sources(_Cfg(["ourcampus", "xior"]), new, [])
        assert [l.id for l in kn] == ["holland2stay"]

    def test_missing_attr_is_tolerated(self):
        """cfg 没有 shadow_sources 属性时不能炸（旧配置对象 / 测试桩）。"""
        class _Bare:
            pass
        new = [_listing("a", source="ourcampus")]
        kn, _ = _drop_shadow_sources(_Bare(), new, [])
        assert kn is new


# ── 配置解析 ────────────────────────────────────────────────────────

class TestShadowConfig:
    def test_parsed_from_env(self, monkeypatch):
        monkeypatch.setenv("SOURCES", "holland2stay,ourcampus")
        monkeypatch.setenv("SHADOW_SOURCES", "ourcampus")
        import config
        assert config.load_config().shadow_sources == ["ourcampus"]

    def test_entries_not_in_sources_are_dropped(self, monkeypatch):
        """影子列表必须是 SOURCES 的子集——不在里面就压根不会抓，写了是笔误。"""
        monkeypatch.setenv("SOURCES", "holland2stay")
        monkeypatch.setenv("SHADOW_SOURCES", "ourcampus,typo")
        import config
        assert config.load_config().shadow_sources == []

    def test_default_is_empty(self, monkeypatch):
        monkeypatch.delenv("SHADOW_SOURCES", raising=False)
        monkeypatch.setenv("SOURCES", "holland2stay")
        import config
        assert config.load_config().shadow_sources == []

    def test_sources_still_defaults_when_empty(self, monkeypatch):
        """拆出 _parse_sources_raw 后，SOURCES 的默认值行为不能变。"""
        monkeypatch.setenv("SOURCES", "")
        import config
        assert config.load_config().sources == ["holland2stay"]


# ── run_once 端到端：入库但不通知 ───────────────────────────────────

class _Notifier(BaseNotifier):
    has_channels = True

    def __init__(self) -> None:
        self.new_listings: list[str] = []
        self.status_changes: list[str] = []

    async def _send(self, text):
        return True

    async def send_new_listing(self, listing):
        self.new_listings.append(listing.id)
        return True

    async def send_status_change(self, listing, old, new):
        self.status_changes.append(listing.id)
        return True

    async def close(self):
        pass


def _cfg(tmp_path, shadow) -> Config:
    return Config(
        check_interval=300,
        cities=[CityFilter(name="C", id=29)],
        availability_filters=[AvailabilityFilter(label="A", id=179)],
        db_path=Path(tmp_path) / "t.db",
        log_level="WARNING",
        sources=["holland2stay", "ourcampus"],
        shadow_sources=shadow,
    )


def _round(tmp_path, storage, shadow, fresh):
    notifier = _Notifier()

    async def go():
        with patch("mcore.scrape_round.dispatch_scrape_tasks", return_value=(fresh, {"C": True})), \
             patch("mcore.prewarm.create_prewarmed_session", return_value=None):
            await run_once(_cfg(tmp_path, shadow), storage, [], dry_run=False,
                           web_notifier=notifier)
    asyncio.run(go())
    return notifier


class TestRunOnceShadow:
    def test_shadow_listing_is_stored_but_not_notified(self, tmp_path):
        storage = Storage(Path(tmp_path) / "t.db", timezone_str="UTC")
        try:
            n = _round(tmp_path, storage, ["ourcampus"], [
                _listing("oc_1", source="ourcampus"),
                _listing("h2s_1", source="holland2stay"),
            ])
            # 通知只发了非影子的那条
            assert n.new_listings == ["h2s_1"]
            # 但两条都进了库
            assert storage.get_listing("oc_1") is not None
            assert storage.get_listing("h2s_1") is not None
            assert sorted(storage.get_distinct_sources()) == ["holland2stay", "ourcampus"]
        finally:
            storage.close()

    def test_shadow_status_change_is_recorded_but_not_notified(self, tmp_path):
        storage = Storage(Path(tmp_path) / "t.db", timezone_str="UTC")
        try:
            _round(tmp_path, storage, ["ourcampus"],
                   [_listing("oc_1", source="ourcampus", status="Occupied")])
            n = _round(tmp_path, storage, ["ourcampus"],
                       [_listing("oc_1", source="ourcampus", status="Available to book")])
            assert n.status_changes == [], "影子 source 的状态变更不该通知"
            # 但库里的状态确实更新了
            assert storage.get_listing("oc_1")["status"] == "Available to book"
        finally:
            storage.close()

    def test_crash_before_notify_does_not_leak_shadow_events(self, tmp_path):
        """重放路径也必须过影子过滤。

        「diff 已提交、通知阶段没走完」的旧事件会在下一轮被重放。如果影子
        过滤排在重放之前，这些事件就绕过保证直接投递——影子源正是不能给
        用户发通知的那个。
        """
        storage = Storage(Path(tmp_path) / "t.db", timezone_str="UTC")
        try:
            _round(tmp_path, storage, ["ourcampus"],
                   [_listing("oc_1", source="ourcampus", status="Occupied")])
            _round(tmp_path, storage, ["ourcampus"],
                   [_listing("oc_1", source="ourcampus",
                             status="Available to book")])

            # 还原成「入库了、还没标记」的崩溃现场
            storage.conn.execute("UPDATE listings SET notified=0")
            storage.conn.execute("UPDATE status_changes SET notified=0")
            storage.conn.commit()

            n = _round(tmp_path, storage, ["ourcampus"], [
                _listing("oc_1", source="ourcampus",
                         status="Available to book"),
            ])
            assert n.new_listings == [], "影子新房源被重放路径投递了"
            assert n.status_changes == [], "影子状态变更被重放路径投递了"
            # 被拦下的事件要标记掉，否则每轮都会重放
            assert storage.pending_new_listings() == []
            assert storage.pending_status_changes() == []
        finally:
            storage.close()

    def test_without_shadow_everything_notifies(self, tmp_path):
        storage = Storage(Path(tmp_path) / "t.db", timezone_str="UTC")
        try:
            n = _round(tmp_path, storage, [], [
                _listing("oc_1", source="ourcampus"),
                _listing("h2s_1", source="holland2stay"),
            ])
            assert sorted(n.new_listings) == ["h2s_1", "oc_1"]
        finally:
            storage.close()

    def test_scrape_count_includes_shadow(self, tmp_path):
        """last_scrape_count 回答「抓到多少」，不是「通知了多少」。"""
        storage = Storage(Path(tmp_path) / "t.db", timezone_str="UTC")
        try:
            _round(tmp_path, storage, ["ourcampus"], [
                _listing("oc_1", source="ourcampus"),
                _listing("h2s_1", source="holland2stay"),
            ])
            assert storage.get_meta("last_scrape_count") == "2"
        finally:
            storage.close()


# ── 悬挂条目：挂在 SHADOW_SOURCES、却不在 SOURCES 里 ─────────────────
#
# 2026-08-04 线上实况：
#
#     SOURCES=holland2stay,ourdomain,xior
#     SHADOW_SOURCES=ourcampus,xior
#
# 看上去像「ourcampus 在影子模式下跑着」。实际上 ``load_config`` 会把不在
# ``sources`` 里的影子项静默丢掉，所以 ourcampus 既没被抓，也不会出现在数据
# 健康面板的最近轮次里——而配置文件读起来完全正常。这个状态是从设置面板
# 保存一次无声造成的（旧版白名单漏了 ourcampus，保存即从 SOURCES 删除）。
#
# 静默丢弃本身是对的（影子名单不该反过来把 source 打开），要补的是那条
# WARNING：配置读起来像开着、实际是关着，这种落差必须有地方能看见。

@pytest.fixture
def _shadow_env(monkeypatch):
    """只留下这几个键，其余清空，免得被开发机 .env 干扰。"""
    for key in ("SOURCES", "SHADOW_SOURCES", "OURCAMPUS_CITIES",
                "OURDOMAIN_CITIES", "XIOR_CITIES", "CITIES"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CITIES", "Eindhoven,29")
    return monkeypatch


class TestDanglingShadowSource:
    def test_warns_when_shadow_source_is_not_in_sources(self, _shadow_env, caplog):
        _shadow_env.setenv("SOURCES", "holland2stay,ourdomain")
        _shadow_env.setenv("SHADOW_SOURCES", "ourcampus")

        with caplog.at_level(logging.WARNING, logger="config"):
            cfg = load_config()

        assert cfg.shadow_sources == []
        assert any("ourcampus" in r.getMessage() for r in caplog.records), \
            "配置读起来像开着、实际关着，必须有 WARNING"

    def test_message_names_every_dangling_source(self, _shadow_env, caplog):
        _shadow_env.setenv("SOURCES", "holland2stay")
        _shadow_env.setenv("SHADOW_SOURCES", "ourcampus,xior")

        with caplog.at_level(logging.WARNING, logger="config"):
            load_config()

        text = " ".join(r.getMessage() for r in caplog.records)
        assert "ourcampus" in text and "xior" in text

    def test_silent_when_shadow_sources_are_all_enabled(self, _shadow_env, caplog):
        """正常配置不该刷警告——否则日志里全是噪音，真出事时看不见。"""
        _shadow_env.setenv("SOURCES", "holland2stay,xior")
        _shadow_env.setenv("SHADOW_SOURCES", "xior")

        with caplog.at_level(logging.WARNING, logger="config"):
            cfg = load_config()

        assert cfg.shadow_sources == ["xior"]
        assert not [r for r in caplog.records if "SHADOW_SOURCES" in r.getMessage()]

    def test_silent_when_shadow_sources_is_empty(self, _shadow_env, caplog):
        _shadow_env.setenv("SOURCES", "holland2stay")
        _shadow_env.delenv("SHADOW_SOURCES", raising=False)

        with caplog.at_level(logging.WARNING, logger="config"):
            cfg = load_config()

        assert cfg.shadow_sources == []
        assert not [r for r in caplog.records if "SHADOW_SOURCES" in r.getMessage()]


class TestShadowStillFiltersProperly:
    def test_dangling_entry_never_enables_the_source(self, _shadow_env):
        """警告归警告，影子名单不能反过来把一个没启用的平台打开。"""
        _shadow_env.setenv("SOURCES", "holland2stay")
        _shadow_env.setenv("SHADOW_SOURCES", "ourcampus")

        cfg = load_config()

        assert "ourcampus" not in cfg.sources
        assert "ourcampus" not in cfg.shadow_sources
        assert "ourcampus" not in {t.source for t in cfg.scrape_tasks_v2()}
