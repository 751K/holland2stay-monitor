"""iOS CI 工作流:模拟器挑选 + 工作流本身的几个前提。

为什么需要这个测试
------------------
本地开发机没装 iOS platform 的模拟器 runtime,``xcodebuild`` 连 destination 都
找不到,Swift 改动只能靠 ``swiftc -typecheck`` 验类型——**一条 iOS 测试都跑不了**。
补上云端跑之后,那条流水线本身就成了唯一的验证入口;它要是悄悄坏了(比如 runner
镜像换了型号、YAML 写错一个缩进),表现是「CI 绿着但什么都没测」。

所以这里守的是挑选逻辑本身,以及工作流里几个一旦写错就会静默失效的地方。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "ios.yml"
SCHEME = (ROOT / "ios/FlatRadar/FlatRadar.xcodeproj/xcshareddata/xcschemes"
          / "FlatRadar.xcscheme")


def _load_picker():
    path = ROOT / "ios" / "scripts" / "pick_simulator.py"
    spec = importlib.util.spec_from_file_location("pick_simulator", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _payload(*entries) -> dict:
    return {"devices": {runtime: devices for runtime, devices in entries}}


def _dev(name, udid, available=True):
    return {"name": name, "udid": udid, "isAvailable": available}


class TestSimulatorPicker:
    def test_picks_the_newest_ios_iphone(self):
        pick = _load_picker().pick
        got = pick(_payload(
            ("com.apple.CoreSimulator.SimRuntime.iOS-17-5", [_dev("iPhone 15", "OLD")]),
            ("com.apple.CoreSimulator.SimRuntime.iOS-18-4", [_dev("iPhone 16", "NEW")]),
        ))
        assert got is not None and got[0] == "NEW"

    def test_version_is_compared_numerically(self):
        """按字符串比的话 "9" > "18",于是永远挑到最旧那台。"""
        pick = _load_picker().pick
        got = pick(_payload(
            ("com.apple.CoreSimulator.SimRuntime.iOS-9-0", [_dev("iPhone 6", "ANCIENT")]),
            ("com.apple.CoreSimulator.SimRuntime.iOS-18-4", [_dev("iPhone 16", "NEW")]),
        ))
        assert got[0] == "NEW"

    def test_unavailable_devices_are_skipped(self):
        """镜像里常留着 runtime 没下全的设备条目。选中它的话,失败发生在启动
        模拟器那一步,错误信息与「代码写错了」几乎分不开。"""
        pick = _load_picker().pick
        got = pick(_payload(
            ("com.apple.CoreSimulator.SimRuntime.iOS-18-4",
             [_dev("iPhone 16 Pro", "BROKEN", available=False),
              _dev("iPhone 16", "GOOD")]),
        ))
        assert got[0] == "GOOD"

    def test_non_ios_runtimes_are_skipped(self):
        """watchOS 的配对伴侣设备名字里也有 iPhone。

        ⚠️ watchOS 的版本号**必须比 iOS 的高**：低的话，就算去掉 runtime 过滤，
        iOS 那台也会因为版本更高而胜出——测试照样是绿的，而过滤器已经没了。
        这条第一版就是那样写的，变异验证时才发现它空过。

        改高之后仍然挡得住**只删 runtime 过滤**这一种变异——因为版本号解析
        （``int(part)`` 对 "watchOS" 抛 ValueError）是第二道防线。两道一起删
        才会红，这一点在验证时确认过；留着这条是为了守住「watchOS 的伴侣设备
        不该被选中」这个行为本身，而不是守某一行代码。
        """
        pick = _load_picker().pick
        got = pick(_payload(
            ("com.apple.CoreSimulator.SimRuntime.watchOS-99-0", [_dev("iPhone (paired)", "W")]),
            ("com.apple.CoreSimulator.SimRuntime.iOS-18-4", [_dev("iPhone 16", "GOOD")]),
        ))
        assert got[0] == "GOOD"

    def test_ipads_are_not_picked(self):
        pick = _load_picker().pick
        got = pick(_payload(
            ("com.apple.CoreSimulator.SimRuntime.iOS-18-4",
             [_dev("iPad Pro 13-inch", "PAD"), _dev("iPhone 16", "PHONE")]),
        ))
        assert got[0] == "PHONE"

    def test_no_simulator_is_an_error_not_an_empty_string(self):
        """挑不出来必须报错退出。返回空串的话 xcodebuild 会拿
        ``-destination "id="`` 去跑,报的错跟项目坏了长得一样。"""
        pick = _load_picker().pick
        assert pick(_payload()) is None

    def test_main_exits_nonzero_without_devices(self, monkeypatch, capsys):
        import io
        module = _load_picker()
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"devices": {}})))
        assert module.main() != 0

    def test_main_prints_tab_separated_udid_first(self, monkeypatch, capsys):
        import io
        module = _load_picker()
        payload = _payload(("com.apple.CoreSimulator.SimRuntime.iOS-18-4",
                            [_dev("iPhone 16", "UDID-1")]))
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        assert module.main() == 0
        out = capsys.readouterr().out.strip()
        # workflow 用 `cut -f1` 取 udid，分隔符必须是 tab
        assert out.split("\t")[0] == "UDID-1"

    def test_malformed_json_is_reported(self, monkeypatch):
        import io
        module = _load_picker()
        monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
        assert module.main() == 2


class TestWorkflow:
    def _text(self) -> str:
        return WORKFLOW.read_text(encoding="utf-8")

    def test_is_valid_yaml(self):
        yaml = pytest.importorskip("yaml")
        assert yaml.safe_load(self._text())

    def test_pipefail_is_set(self):
        """没有 pipefail 的话，xcodebuild 失败会被后面的格式化工具吞掉，
        CI 绿着而测试其实是红的——比不跑 CI 更糟。"""
        assert "set -o pipefail" in self._text()

    def test_runs_the_unit_test_target(self):
        t = self._text()
        assert "-only-testing:FlatRadarTests" in t

    def test_does_not_hardcode_a_device_name(self):
        """写死型号的话，runner 镜像一换就报 no destinations。"""
        t = self._text()
        assert "name=iPhone" not in t
        assert "pick_simulator.py" in t

    def test_uploads_the_result_bundle(self):
        t = self._text()
        assert "xcresult" in t and "upload-artifact" in t

    def test_asserts_that_tests_actually_ran(self):
        """核心：``xcodebuild test`` 一条用例都没跑时同样报 Test Succeeded、
        同样退出 0。第一次跑这条流水线就是这个情形——日志里没有任何用例名，
        而 CI 是绿的。只看退出码分不出「全过了」和「压根没跑」。
        """
        t = self._text()
        assert "summarize_xcresult.py" in t, "没有任何一步核对执行条数"


class TestResultSummary:
    def _load(self):
        import importlib.util
        path = ROOT / "ios" / "scripts" / "summarize_xcresult.py"
        spec = importlib.util.spec_from_file_location("summarize_xcresult", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_zero_tests_is_a_failure(self, monkeypatch, capsys):
        m = self._load()
        monkeypatch.setattr(m, "summary", lambda _p: {"totalTestCount": 0,
                                                      "passedTests": 0, "failedTests": 0})
        monkeypatch.setattr("sys.argv", ["x", "dummy.xcresult"])
        assert m.main() != 0, "一条没跑也算通过的话，这个守卫等于不存在"

    def test_failures_are_a_failure(self, monkeypatch):
        m = self._load()
        monkeypatch.setattr(m, "summary", lambda _p: {"totalTestCount": 5,
                                                      "passedTests": 4, "failedTests": 1})
        monkeypatch.setattr("sys.argv", ["x", "dummy.xcresult"])
        assert m.main() == 1

    def test_unreadable_bundle_is_a_failure(self, monkeypatch):
        """读不出来时判失败而不是放行——放行的话，xcresulttool 哪天换了子命令，
        这个守卫会在没人察觉的情况下退化成永远通过。"""
        m = self._load()
        monkeypatch.setattr(m, "summary", lambda _p: None)
        monkeypatch.setattr("sys.argv", ["x", "dummy.xcresult"])
        assert m.main() != 0

    def test_a_healthy_run_passes(self, monkeypatch, capsys):
        m = self._load()
        monkeypatch.setattr(m, "summary", lambda _p: {"totalTestCount": 60,
                                                      "passedTests": 60, "failedTests": 0})
        monkeypatch.setattr("sys.argv", ["x", "dummy.xcresult"])
        assert m.main() == 0
        assert "60" in capsys.readouterr().out


class TestSchemeActuallyRunsUnitTests:
    """``xcodebuild test`` 必须真的执行到单元测试。

    守的是「绿着，但是空的」：Testables 里只有 FlatRadarUITests 的那阵子，
    单元测试能编译、bundle 能链接、xcodebuild 报 Test Succeeded 退出 0，
    执行条数是 0。

    2026-09 加了 .xctestplan 之后，这条链路变了：scheme 的 TestAction 不再
    直接列 target，而是指向一个 test plan，target 写在 plan 里。原来那句
    ``assert 'BlueprintName = "FlatRadarTests"' in xml`` 从此查的是一个不再
    存在的结构——它是真的红了，不是误报，但它红的理由和它想守的东西已经没
    关系了。所以顺着新的链路重写：scheme → 默认 plan → testTargets。
    """

    @staticmethod
    def _default_plan_path():
        """scheme 的 TestAction 里标了 ``default="YES"`` 的那个 plan。

        ``container:`` 相对于 .xcodeproj 所在的目录。SCHEME 是
        ``<项目目录>/FlatRadar.xcodeproj/xcshareddata/xcschemes/FlatRadar.xcscheme``，
        往上数四层正好是项目目录——这样单仓库和独立仓库两种布局都不用改。
        """
        import re

        xml = SCHEME.read_text(encoding="utf-8")
        action = re.search(r"<TestAction.*?</TestAction>", xml, re.S)
        assert action, "scheme 里没有 TestAction —— xcodebuild test 无事可做"

        refs = re.findall(r"<TestPlanReference\s+(.*?)>", action.group(0), re.S)
        assert refs, ("TestAction 里既没有 TestPlans 也没有 Testables；"
                      "这种 scheme 跑 test 会是空的")

        default = [r for r in refs if 'default = "YES"' in r]
        assert len(default) == 1, (
            f"应当恰好有一个 default plan，实际 {len(default)} 个。"
            "没有默认 plan 时，不带 -testPlan 的 xcodebuild test 行为不确定。")

        ref = re.search(r'reference = "container:([^"]+)"', default[0])
        assert ref, "默认 TestPlanReference 上没有 container: 路径"
        return SCHEME.parents[3] / ref.group(1)

    def test_default_test_plan_exists(self):
        plan = self._default_plan_path()
        assert plan.is_file(), (
            f"scheme 指向的默认 test plan 不存在：{plan}。"
            "路径写错时 Xcode 不会报错，只会当作没有测试。")

    def test_unit_test_target_is_in_the_default_plan(self):
        import json

        plan = json.loads(self._default_plan_path().read_text(encoding="utf-8"))
        targets = {t["target"]["name"]: t for t in plan.get("testTargets", [])}

        assert "FlatRadarTests" in targets, (
            f"默认 test plan 里没有 FlatRadarTests，只有 {sorted(targets)}。"
            "这样 xcodebuild test 会绿，但一条单元测试都不跑。")

        # plan 里可以把某个 target 标成 enabled: false —— 它仍然列在那里，
        # 只是不执行。缺了这一句，禁用和启用在测试眼里是一样的。
        assert targets["FlatRadarTests"].get("enabled", True), \
            "FlatRadarTests 在默认 test plan 里被禁用了"

    def test_scheme_is_shared(self):
        """必须在 xcshareddata 下，否则新克隆的仓库里 xcodebuild 找不到它。"""
        assert SCHEME.exists()
        assert "xcshareddata" in str(SCHEME)


class TestUnitTestTargetHasSources:
    """``FlatRadarTests`` 必须挂着它自己那个目录，否则整个 target 是空的。

    这是「CI 绿着但一条没跑」的**根因**，而且它同时解释了另外两件怪事：

    - ``ListingTests.swift`` 里引用了 ``Listing`` 上根本不存在的成员，却从来
      没让构建失败——因为那个文件压根没被编译；
    - 测试 bundle 链接成功、``xcodebuild test`` 报 Test Succeeded、退出 0，
      而执行条数是 0。

    工程用的是 Xcode 16+ 的目录同步（``PBXFileSystemSynchronizedRootGroup``）：
    另外两个 target 都挂了自己的目录，唯独 ``FlatRadarTests`` 漏了。漏掉之后
    没有任何报错——它只是安静地什么都不编译。
    """

    PBXPROJ = ROOT / "ios/FlatRadar/FlatRadar.xcodeproj/project.pbxproj"

    def _text(self) -> str:
        return self.PBXPROJ.read_text(encoding="utf-8")

    def test_the_folder_is_declared_as_a_synchronized_group(self):
        import re

        text = self._text()
        section = text[text.index("Begin PBXFileSystemSynchronizedRootGroup"):
                       text.index("End PBXFileSystemSynchronizedRootGroup")]
        paths = set(re.findall(r"path = (\w+);", section))
        assert "FlatRadarTests" in paths, f"同步组里没有测试目录：{paths}"

    def test_the_target_actually_uses_it(self):
        """光声明不够——必须挂在 target 的 fileSystemSynchronizedGroups 上。"""
        import re

        text = self._text()
        m = re.search(
            r"/\* FlatRadarTests \*/ = \{\s*isa = PBXNativeTarget;(.*?)\n\t\t\};",
            text, re.S)
        assert m, "找不到 FlatRadarTests target"
        assert "fileSystemSynchronizedGroups" in m.group(1), (
            "target 没挂同步目录——它会编译出一个空的 xctest bundle")

    def test_every_test_target_is_wired_the_same_way(self):
        """三个 target 一视同仁。漏掉哪一个，那一个就静默变空。"""
        import re

        text = self._text()
        for name in ("FlatRadar", "FlatRadarUITests", "FlatRadarTests"):
            m = re.search(
                r"/\* " + name + r" \*/ = \{\s*isa = PBXNativeTarget;(.*?)\n\t\t\};",
                text, re.S)
            assert m and "fileSystemSynchronizedGroups" in m.group(1), name


class TestLaunchArgVocabulariesAgree:
    """截图测试发出去的 ``UI_TEST_TAB`` / ``UI_TEST_BROWSE_MODE``，App 那边得认识。

    这两个 launch arg 用的是**两套词表**：

        UI_TEST_BROWSE_MODE ∈ { list,     map, calendar }
        UI_TEST_TAB         ∈ { listings, map, calendar, dashboard, ... }

    只有 list / listings 这一个词不同，map 和 calendar 两边同名。所以把 mode
    直接当 tab 名发过去时，02-Listings 每轮都挂、03-Map 和 04-Calendar 每轮都过
    ——看起来完全像是 Listings 那一条有偶发问题，而不是拼写对不上。

    实际发生过：``browse(_:padTab:)`` 的 iPad 分支发的是 ``UI_TEST_TAB=list``，
    ContentView 的 switch 认的是 ``"listings"``，落进 ``default`` 静默放过，tab
    原地不动，截图照拍——名字对、尺寸对、内容是 Dashboard。云端跑一轮才知道，
    而失败信息只说「选中的不是 Listings」。

    这条测试在本地 0.2 秒内回答同一个问题。

    ⚠️ 第一版有两个洞，都属于「看着对、但永远不会红」：

    1. 取 switch 内容时按固定字符数截，跨到了下一个 switch 里，于是
       BROWSE_MODE 的 ``"list"`` 被算进「App 认识的 tab 名」，要查的分歧被自己
       抹平了。现在按 ``argValue(`` 的下一次出现切断。
    2. 用 ``tab-[a-z]+`` 抓 Tab 的 id，抓不到的（含数字、下划线……）**被静默
       跳过**，集合里少一个自然没有分歧。现在先数一遍 ``static let`` 的个数，
       解析出来的对不上就直接失败——解析器漏了东西，不能表现成「检查通过」。
    """

    SCREENSHOT_TESTS = ROOT / "ios/FlatRadar/FlatRadarUITests/ScreenshotTests.swift"
    CONTENT_VIEW = ROOT / "ios/FlatRadar/FlatRadar/Views/ContentView.swift"

    @classmethod
    def _switch_cases(cls, marker: str) -> set[str]:
        """``argValue("<marker>", …)`` 那个 switch 里的 case 字面量。

        边界切在下一个 ``argValue(`` 上。按固定长度截会跨进下一个 switch，
        而那正好是另一套词表——两套一混，这个测试就废了。
        """
        import re

        text = cls.CONTENT_VIEW.read_text(encoding="utf-8")
        start = text.index(f'argValue("{marker}"')
        nxt = text.find("argValue(", start + 1)
        block = text[start:nxt if nxt != -1 else len(text)]
        cases = set(re.findall(r'case "([^"]+)"', block))
        assert cases, f"{marker} 后面没解析出任何 case"
        return cases

    @classmethod
    def _tab_launch_names(cls) -> set[str]:
        """每个 ``Tab`` 的 launchName：id 去掉 ``tab-`` 前缀，alerts 特判。

        跟 Swift 那边两行实现保持一致。这里重算而不是解析实现本身——实现只有
        两行，重算比解析可靠；真有分歧，下面的断言会指出来。
        """
        import re

        text = cls.SCREENSHOT_TESTS.read_text(encoding="utf-8")
        declared = re.findall(r"static let (\w+)\s*=\s*Tab\(", text)
        ids = re.findall(r'Tab\(id: "([^"]+)"', text)
        assert declared, "没在 ScreenshotTests 里找到 static let … = Tab(…)"
        # 解析器漏掉一个，就会少比一个名字，然后这条测试「通过」。
        assert len(ids) == len(declared), (
            f"解析到 {len(ids)} 个 Tab id，但声明了 {len(declared)} 个"
            f"（{declared}）——正则漏了东西，这时候不能算检查通过")
        for tab_id in ids:
            assert tab_id.startswith("tab-"), f"Tab id 不以 tab- 开头：{tab_id}"
        return {"notifications" if i == "tab-alerts" else i[len("tab-"):]
                for i in ids}

    def test_every_tab_launch_name_is_handled_by_the_app(self):
        handled = self._switch_cases("UI_TEST_TAB")
        unknown = self._tab_launch_names() - handled
        assert not unknown, (
            f"ScreenshotTests 会发出这些 UI_TEST_TAB 值，但 ContentView 的 "
            f"switch 不认识：{sorted(unknown)}。App 认识的是 {sorted(handled)}。"
            "对不上就落进 default，tab 原地不动，而截图照拍。")

    def test_ipad_sends_tab_names_not_browse_modes(self):
        """``browse(_:padTab:)`` 的 iPad 分支必须发 tab 名，不能发 mode 名。

        这是那个 bug 的原始形态：两个参数长得一样，写错了编译照过。
        """
        import re

        text = self.SCREENSHOT_TESTS.read_text(encoding="utf-8")
        start = text.index("private func browse(")
        body = text[start:text.index("\n    }", start)]
        ipad_line = [ln for ln in body.splitlines() if "UI_TEST_TAB=" in ln
                     and "BROWSE_MODE" not in ln]
        assert ipad_line, "browse(…) 里没找到 iPad 那支的 UI_TEST_TAB="
        assert not re.search(r"UI_TEST_TAB=\\\(mode\)", ipad_line[0]), (
            "iPad 分支把 browse mode 当 tab 名发出去了。mode 的词表是 "
            "{list, map, calendar}，tab 的是 {listings, map, calendar}——"
            "只有 list/listings 不同，所以只有 Listings 会挂，看着像偶发。")

    def test_browse_modes_are_handled_by_the_app(self):
        import re

        text = self.SCREENSHOT_TESTS.read_text(encoding="utf-8")
        sent = set(re.findall(r'browse\("([a-z]+)"', text))
        assert sent, "没在 ScreenshotTests 里找到 browse(…) 的调用"
        handled = self._switch_cases("UI_TEST_BROWSE_MODE")
        unknown = sent - handled
        assert not unknown, (
            f"这些 browse mode 发得出去但 App 不认识：{sorted(unknown)}，"
            f"App 认识的是 {sorted(handled)}")

    def test_unknown_values_are_not_silently_ignored(self):
        """``default: break`` 是这个 bug 能活下来的原因。

        拼错一个值不会有任何提示——没有日志、没有崩溃，只有一张内容不对的图。
        改成 assertionFailure 之后，Debug 构建（截图就是 Debug）会当场停下。
        """
        for marker in ("UI_TEST_TAB", "UI_TEST_BROWSE_MODE"):
            block_cases = self._switch_cases(marker)
            assert block_cases  # 上面已断言非空，这里只是让意图明确
            text = self.CONTENT_VIEW.read_text(encoding="utf-8")
            start = text.index(f'argValue("{marker}"')
            nxt = text.find("argValue(", start + 1)
            block = text[start:nxt if nxt != -1 else len(text)]
            assert "assertionFailure" in block, (
                f"{marker} 的 switch 里没有 assertionFailure —— "
                "不认识的值会被静默放过，然后拍出一张内容不对的截图")
