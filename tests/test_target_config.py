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


class TestRegistryDrift:
    """注册表加了城市、订阅串没跟上。

    2026-09-08 Plaza 上架 Rijswijk 那次暴露的第三个副本：注册表是菜单，订阅串
    才是点的菜。把城市加进注册表会让 scraper 那条「未登记城市」的 WARNING 闭嘴，
    但不会让它被抓——唯一的痕迹被修复动作本身抹掉了。
    """

    def test_first_run_reports_nothing(self):
        """没有快照时只记录不报。

        这条不是「宽容一点」，是**不报假警**：实测生效配置里 holland2stay 26 城
        订 2 个、Xior 30 栋订 4 栋，都是明确的选择。首轮把它们全报出来，50 条里
        0 条是真的，然后没人再看这类提醒。
        """
        problems, snapshot = tc.registry_drift({}, None)
        assert problems == []
        assert "PLAZA_CITIES" in snapshot and snapshot["PLAZA_CITIES"]

    def test_a_deliberate_subset_is_not_drift(self):
        """只订两个城市不是漂移——快照里已经有的，不管订没订都不报。"""
        seen = {k: sorted(tc.registry_of(k)) for k in tc.DRIFT_KEYS if tc.registry_of(k)}
        env = {"CITIES": "Amsterdam,24|Eindhoven,29"}
        problems, _ = tc.registry_drift(env, seen)
        assert problems == []

    def test_a_new_registry_entry_that_is_not_subscribed_is_reported(self):
        """注册表新增、订阅串没有 → 报。这就是 Rijswijk 那一次。"""
        seen = {k: sorted(tc.registry_of(k)) for k in tc.DRIFT_KEYS if tc.registry_of(k)}
        seen["PLAZA_CITIES"] = [k for k in seen["PLAZA_CITIES"] if k != "rijswijk"]
        env = {"PLAZA_CITIES": "|".join(
            f"{n},{k}" for k, n in tc.registry_of("PLAZA_CITIES").items()
            if k != "rijswijk")}

        problems, _ = tc.registry_drift(env, seen)
        assert len(problems) == 1
        assert problems[0].key == "PLAZA_CITIES"
        assert "Rijswijk" in str(problems[0])
        assert not problems[0].fatal      # 不该挡住启动

    def test_a_new_entry_that_is_subscribed_is_not_reported(self):
        """新增但已经勾上了 → 不报。漂移的定义是「没跟上」，不是「变了」。"""
        seen = {k: sorted(tc.registry_of(k)) for k in tc.DRIFT_KEYS if tc.registry_of(k)}
        seen["PLAZA_CITIES"] = [k for k in seen["PLAZA_CITIES"] if k != "rijswijk"]
        env = {"PLAZA_CITIES": "|".join(
            f"{n},{k}" for k, n in tc.registry_of("PLAZA_CITIES").items())}

        problems, _ = tc.registry_drift(env, seen)
        assert problems == []

    def test_unhandled_additions_stay_out_of_the_snapshot(self):
        """报过一次不算处理过。

        把新增项写进快照的话，重启一次警告就永远消失了，而城市还是没被抓——那正是
        这条检查要防的那种「痕迹被抹掉」。所以未处理的新增要留在快照外面，下次启动
        再说一遍。
        """
        seen = {k: sorted(tc.registry_of(k)) for k in tc.DRIFT_KEYS if tc.registry_of(k)}
        seen["PLAZA_CITIES"] = [k for k in seen["PLAZA_CITIES"] if k != "rijswijk"]
        env = {"PLAZA_CITIES": "Utrecht,utrecht"}

        first, snap = tc.registry_drift(env, seen)
        assert len(first) == 1
        assert "rijswijk" not in snap["PLAZA_CITIES"]

        second, _ = tc.registry_drift(env, snap)      # 拿新快照再跑一次
        assert len(second) == 1, "重启一次就不再提醒了"

    def test_saving_settings_acknowledges_everything(self):
        """设置页保存 = 看过整张菜单，此后不再提醒。"""
        env = {"PLAZA_CITIES": "Utrecht,utrecht"}
        seen = {k: sorted(tc.registry_of(k)) for k in tc.DRIFT_KEYS if tc.registry_of(k)}
        seen["PLAZA_CITIES"] = [k for k in seen["PLAZA_CITIES"] if k != "rijswijk"]
        assert tc.registry_drift(env, seen)[0]           # 先确认本来会报

        problems, _ = tc.registry_drift(env, tc.ack_registry())
        assert problems == []

    def test_empty_means_all_for_the_platforms_that_say_so(self):
        """空串对 plaza / magis / se / xior 是「全选」，不是「一个都没订」。

        搞混的后果是每加一个城市都报一次假警——而那些平台留空恰恰表示「全都要」，
        新城市本来就自动包含在内。
        """
        for key in ("PLAZA_CITIES", "MAGIS_CITIES",
                    "STUDENTEXPERIENCE_CITIES", "XIOR_CITIES"):
            assert tc.subscribed_keys(key, "") == set(tc.registry_of(key)), key
        # CITIES / OURDOMAIN_CITIES 的空串是真的空
        assert tc.subscribed_keys("CITIES", "") == set()
        assert tc.subscribed_keys("OURDOMAIN_CITIES", "") == set()

    def test_empty_all_platforms_never_drift(self):
        """留空的平台永远不该报漂移——新城市自动就在订阅里。"""
        seen = {k: sorted(tc.registry_of(k)) for k in tc.DRIFT_KEYS if tc.registry_of(k)}
        seen["PLAZA_CITIES"] = [k for k in seen["PLAZA_CITIES"] if k != "rijswijk"]
        problems, _ = tc.registry_drift({"PLAZA_CITIES": ""}, seen)
        assert problems == []

    def test_a_newly_added_source_reports_nothing_on_its_first_run(self):
        """接新平台的那一轮，它整张表都是「新增」，全报出来没有意义。"""
        seen = {k: sorted(tc.registry_of(k)) for k in tc.DRIFT_KEYS if tc.registry_of(k)}
        del seen["PLAZA_CITIES"]
        problems, snap = tc.registry_drift({"PLAZA_CITIES": "Utrecht,utrecht"}, seen)
        assert problems == []
        assert snap["PLAZA_CITIES"] == sorted(tc.registry_of("PLAZA_CITIES"))

    def test_the_meta_key_has_exactly_one_definition(self):
        """monitor 和设置页都写这个键，名字只能有一份。"""
        import pathlib
        import re
        literal = re.compile(r'["\']registry_targets_seen["\']')
        hits = [f.name for f in (pathlib.Path("monitor.py"),
                                 pathlib.Path("app/routes/settings.py"))
                if literal.search(f.read_text())]
        assert hits == [], f"这些文件写死了 meta 键名，应该用常量：{hits}"

    def test_all_when_empty_matches_what_config_actually_does(self, monkeypatch):
        """``_ALL_WHEN_EMPTY`` 必须和 config.load_config() 的真实行为一致。

        这张表是手抄的一份约定。抄错的后果是安静的：把「空=全选」的键漏掉，那个
        平台每加一个城市都会报一次假警；把「空=不抓」的键错列进去，真漂移反而不报。
        所以不比对文档，直接问 load_config——留空之后它到底给出几个目标。
        """
        from config import load_config

        attr = {
            "CITIES": "cities",
            "OURDOMAIN_CITIES": "ourdomain_cities",
            "OURCAMPUS_CITIES": "ourcampus_cities",
            "XIOR_CITIES": "xior_cities",
            "MAGIS_CITIES": "magis_cities",
            "STUDENTEXPERIENCE_CITIES": "studentexperience_cities",
            "PLAZA_CITIES": "plaza_cities",
        }
        source_of_key = {"CITIES": "holland2stay", **tc.TARGET_KEYS}

        for key, field in attr.items():
            monkeypatch.setenv("SOURCES", source_of_key[key])
            monkeypatch.setenv(key, "")
            got = len(getattr(load_config(), field))
            empty_means_all = got == len(tc.registry_of(key)) and got > 0
            assert (key in tc._ALL_WHEN_EMPTY) is empty_means_all, (
                f"{key}：留空时 load_config 给出 {got} 个目标，"
                f"注册表有 {len(tc.registry_of(key))} 个，"
                f"但 _ALL_WHEN_EMPTY 里{'有' if key in tc._ALL_WHEN_EMPTY else '没有'}它"
            )
