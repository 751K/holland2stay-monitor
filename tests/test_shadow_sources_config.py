"""SHADOW_SOURCES 里挂着 SOURCES 没有的 source 时必须出声。

2026-08-04 线上实况：

    SOURCES=holland2stay,ourdomain,xior
    SHADOW_SOURCES=ourcampus,xior

看上去像"ourcampus 在影子模式下跑着"。实际上 ``load_config`` 会把不在
``sources`` 里的影子项静默丢掉,所以 ourcampus 既没被抓,也不会出现在数据
健康面板的最近轮次里——而配置文件读起来完全正常。这个状态是从设置面板
保存一次无声造成的（旧版白名单漏了 ourcampus,保存即从 SOURCES 删除）。

静默丢弃本身是对的（影子名单不该反过来把 source 打开），要补的是那条
WARNING：配置读起来像开着、实际是关着,这种落差必须有地方能看见。
"""
from __future__ import annotations

import logging

import pytest

from config import load_config


@pytest.fixture
def env(monkeypatch):
    """只留下这个测试关心的几个键,其余清空,免得被开发机 .env 干扰。"""
    for key in ("SOURCES", "SHADOW_SOURCES", "OURCAMPUS_CITIES",
                "OURDOMAIN_CITIES", "XIOR_CITIES", "CITIES"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CITIES", "Eindhoven,29")
    return monkeypatch


class TestDanglingShadowSource:
    def test_warns_when_shadow_source_is_not_in_sources(self, env, caplog):
        env.setenv("SOURCES", "holland2stay,ourdomain")
        env.setenv("SHADOW_SOURCES", "ourcampus")

        with caplog.at_level(logging.WARNING, logger="config"):
            cfg = load_config()

        assert cfg.shadow_sources == []
        assert any("ourcampus" in r.getMessage() for r in caplog.records), \
            "配置读起来像开着、实际关着，必须有 WARNING"

    def test_message_names_every_dangling_source(self, env, caplog):
        env.setenv("SOURCES", "holland2stay")
        env.setenv("SHADOW_SOURCES", "ourcampus,xior")

        with caplog.at_level(logging.WARNING, logger="config"):
            load_config()

        text = " ".join(r.getMessage() for r in caplog.records)
        assert "ourcampus" in text and "xior" in text

    def test_silent_when_shadow_sources_are_all_enabled(self, env, caplog):
        """正常配置不该刷警告——否则日志里全是噪音，真出事时看不见。"""
        env.setenv("SOURCES", "holland2stay,xior")
        env.setenv("SHADOW_SOURCES", "xior")

        with caplog.at_level(logging.WARNING, logger="config"):
            cfg = load_config()

        assert cfg.shadow_sources == ["xior"]
        assert not [r for r in caplog.records if "SHADOW_SOURCES" in r.getMessage()]

    def test_silent_when_shadow_sources_is_empty(self, env, caplog):
        env.setenv("SOURCES", "holland2stay")
        env.delenv("SHADOW_SOURCES", raising=False)

        with caplog.at_level(logging.WARNING, logger="config"):
            cfg = load_config()

        assert cfg.shadow_sources == []
        assert not [r for r in caplog.records if "SHADOW_SOURCES" in r.getMessage()]


class TestShadowStillFiltersProperly:
    def test_dangling_entry_never_enables_the_source(self, env):
        """警告归警告，影子名单不能反过来把一个没启用的平台打开。"""
        env.setenv("SOURCES", "holland2stay")
        env.setenv("SHADOW_SOURCES", "ourcampus")

        cfg = load_config()

        assert "ourcampus" not in cfg.sources
        assert "ourcampus" not in cfg.shadow_sources
        assert "ourcampus" not in {t.source for t in cfg.scrape_tasks_v2()}
