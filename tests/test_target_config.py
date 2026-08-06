"""塞在字符串里的结构化配置：解析失败必须说话。

监控范围本质是表格，却被压成带分隔符的字符串。2026-08-06 实测，同一类输入错误
的后果毫无一致性：

    CITIES=Eindhoven          漏 ID     → 静默丢弃，0 个城市，monitor 照常跑
    CITIES=Eindhoven;29       分隔符错   → 同上
    CITIES=Eindhoven,abc      ID 非数字  → ValueError，monitor 起不来
    AVAILABILITY_FILTERS=…,999999       → 照单全收，抓一个不存在的状态
    SOURCES=holland2stay,xiorr          → 照单全收，一个不存在的平台

最糟的是第一种。空列表是**合法配置**（「不监控任何城市」），所以没有任何地方会
报错——监控正常启动、正常跑轮次、一条房源都不抓。

本文件的用例直接取自那次实测，一个不落。
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

import target_config as tc


def _fatal(problems):
    return [p for p in problems if p.fatal]


class TestFormatErrorsAreFatal:
    """格式坏了一定是错的：没有哪种正确配置长这样。"""

    @pytest.mark.parametrize("key,value,why", [
        ("CITIES", "Eindhoven", "漏了 ID"),
        ("CITIES", "Eindhoven;29", "分号不是分隔符"),
        ("CITIES", "Eindhoven,abc", "ID 不是整数"),
        ("CITIES", ",29", "城市名为空"),
        ("AVAILABILITY_FILTERS", "Available to book,179|Reserved", "第二项漏了 ID"),
        ("AVAILABILITY_FILTERS", "Reserved,x", "ID 不是整数"),
        ("XIOR_CITIES", "只有名字", "漏了 key"),
        ("SHARD_SIZES", "xior4", "漏了冒号"),
        ("SHARD_SIZES", "xior:abc", "值不是整数"),
        ("SHARD_SIZES", "xior:-1", "负数"),
        ("SOURCE_MIN_INTERVALS", "xior 180", "漏了冒号"),
    ])
    def test_rejected(self, key, value, why):
        problems = tc.validate({key: value})
        assert _fatal(problems), f"{why} 没被判为致命：{value!r}"

    def test_message_names_the_offending_entry(self):
        """只说「CITIES 有问题」等于没说——一条里有五个城市，是哪个？"""
        [p] = _fatal(tc.validate({"CITIES": "Eindhoven,29|Amsterdam"}))
        assert "Amsterdam" in str(p)
        assert "Eindhoven" not in str(p), "把好的那项也报进来了"


class TestUnknownEntitiesAreWarningsNotErrors:
    """实体不认识只是警告：官方注册表会更新，写死拒绝会让新城市变成启动失败。"""

    @pytest.mark.parametrize("key,value", [
        ("CITIES", "SomeNewTown,9999"),
        ("AVAILABILITY_FILTERS", "X,999999"),
        ("SOURCES", "holland2stay,xiorr"),
        ("XIOR_CITIES", "某楼,p9999999"),
        ("SHARD_SIZES", "nosuchsource:4"),
    ])
    def test_warned_but_not_fatal(self, key, value):
        problems = tc.validate({key: value})
        assert problems, f"{value!r} 一声不吭"
        assert not _fatal(problems), f"{value!r} 不该阻止保存"

    def test_name_id_mismatch_is_caught(self):
        """ID 对得上但名字写错——面板不会产生，手改会。"""
        [p] = tc.validate({"CITIES": "Amsterdam,29"})
        assert "Eindhoven" in str(p) and not p.fatal


class TestValidConfigIsSilent:
    @pytest.mark.parametrize("key,value", [
        ("CITIES", "Eindhoven,29|Amsterdam,24"),
        ("CITIES", "Eindhoven,29|"),          # 尾部多一个分隔符是手写常见笔误
        ("CITIES", ""),                        # 空 = 不监控，是合法配置
        ("SOURCES", "holland2stay,ourdomain,xior"),
        ("AVAILABILITY_FILTERS", "Available to book,179|Reserved,6203"),
        ("SHARD_SIZES", "xior:4"),
        ("SHARD_SIZES", "xior:0"),             # 0 = 关掉分片
        ("SOURCE_MIN_INTERVALS", "xior:180"),
        ("XIOR_CITIES", "Eindhoven Kronehoefstraat,p0196467"),
    ])
    def test_no_problems(self, key, value):
        assert tc.validate({key: value}) == []

    def test_the_real_production_values(self):
        """生产在用的这一组必须干净，否则升级上去满屏 ERROR。"""
        assert tc.validate({
            "SOURCES": "holland2stay,ourdomain,ourcampus,xior",
            "CITIES": "Eindhoven,29|Amsterdam,24",
            "OURDOMAIN_CITIES": "Amsterdam Diemen,diemen",
            "OURCAMPUS_CITIES": "OurCampus Amsterdam Diemen,diemen",
            "AVAILABILITY_FILTERS":
                "Available to book,179|Available in lottery,336|Reserved,6203",
            "SHARD_SIZES": "xior:4",
            "SOURCE_MIN_INTERVALS": "xior:180",
        }) == []


class TestTheSilentZeroCities:
    """整条 CITIES 解析不出东西 → H2S 什么都不抓，而且没有任何地方会报错。

    这是本次要修的核心：空列表是合法配置，所以「解析全军覆没」和「你就是不想监控
    任何城市」在下游完全无法区分。只有在解析这一层才分得出来。
    """

    def test_all_entries_broken_is_reported(self):
        problems = tc.validate_effective({"CITIES": "Eindhoven;29|Amsterdam"})
        assert any("整条都没解析出城市" in str(p) for p in problems)

    def test_deliberately_empty_is_not_reported(self):
        """真的不想监控 H2S 时不该被骚扰。"""
        problems = tc.validate_effective({"CITIES": ""})
        assert not any("整条都没解析出城市" in str(p) for p in problems)

    def test_partially_broken_does_not_trigger_it(self):
        """还剩至少一个城市时，报的是那一项，不是「整条」。"""
        problems = tc.validate_effective({"CITIES": "Eindhoven,29|Amsterdam"})
        assert any("Amsterdam" in str(p) for p in problems)
        assert not any("整条都没解析出城市" in str(p) for p in problems)


class TestRoundTrip:
    """解析与生成必须对得上——面板和迁移都要往回写。"""

    @pytest.mark.parametrize("value", [
        "Eindhoven,29|Amsterdam,24",
        "Available to book,179|Reserved,6203",
    ])
    def test_pairs_survive(self, value):
        parsed, problems = (tc.parse_cities(value) if "Eindhoven" in value
                            else tc.parse_availability(value))
        assert not problems
        assert tc.format_pairs(parsed) == value

    def test_source_map_survives(self):
        parsed, problems = tc.parse_source_map("SHARD_SIZES", "xior:4")
        assert not problems
        assert tc.format_source_map(parsed) == "xior:4"


class TestEveryStructuredKeyHasAParser:
    """漏掉一个键，它就退回到「静默丢弃」的老样子。"""

    def test_covers_all_delimiter_packed_runtime_keys(self):
        from env_registry import RUNTIME_KEYS

        # runtime 里剩下的都是标量（间隔、时间点、开关），不需要解析
        packed = {
            "SOURCES", "SHADOW_SOURCES", "CITIES",
            "OURDOMAIN_CITIES", "OURCAMPUS_CITIES", "XIOR_CITIES",
            "AVAILABILITY_FILTERS", "SHARD_SIZES", "SOURCE_MIN_INTERVALS",
        }
        assert packed <= RUNTIME_KEYS
        assert packed == set(tc.STRUCTURED_KEYS)

    def test_unknown_keys_are_ignored_not_crashed(self):
        assert tc.validate({"CHECK_INTERVAL": "300"}) == []


class TestWiredIn:
    """校验挂在两个入口上，否则它只是一个没人调的纯函数。"""

    def test_panel_validates_before_writing(self):
        src = (Path(__file__).resolve().parent.parent
               / "app" / "routes" / "settings.py").read_text(encoding="utf-8")
        i = src.index("validate_structured(pending)")
        j = src.index("set_app_settings(pending")
        assert i < j, "先写库后校验，坏值已经进去了"

    def test_monitor_self_checks_at_startup(self):
        import monitor

        assert "validate_effective" in inspect.getsource(monitor._validate_structured_config)
        assert "_validate_structured_config()" in inspect.getsource(monitor._bootstrap_settings)

    def test_startup_check_never_blocks(self):
        """一个配置笔误让整个监控停摆，代价远大于笔误本身。"""
        import monitor

        src = inspect.getsource(monitor._validate_structured_config)
        assert "raise" not in src
        assert "sys.exit" not in src
