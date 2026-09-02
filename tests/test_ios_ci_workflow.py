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

    def test_uploads_the_result_bundle_on_failure(self):
        t = self._text()
        assert "xcresult" in t and "if: failure()" in t


class TestSchemeActuallyRunsUnitTests:
    def test_unit_test_target_is_in_the_scheme(self):
        """``xcodebuild test`` 只跑 scheme 的 TestAction 里列出的 target。

        此前 Testables 里**只有 FlatRadarUITests**——就算单元测试能编译，
        也一条都不会执行。这和「测试挂了」不同：它是绿的，只是空的。
        """
        xml = SCHEME.read_text(encoding="utf-8")
        assert 'BlueprintName = "FlatRadarTests"' in xml

    def test_scheme_is_shared(self):
        """必须在 xcshareddata 下，否则新克隆的仓库里 xcodebuild 找不到它。"""
        assert SCHEME.exists()
        assert "xcshareddata" in str(SCHEME)
