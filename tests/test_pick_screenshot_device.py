"""``ios/scripts/pick_screenshot_device.py``。

挑错机型的后果不是构建失败，是产出一批尺寸不对的 PNG——ASC 在上传那一步才
拒绝，而拒绝信息只说「尺寸不符」，不说该用哪台。所以挑选规则本身要有测试。
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "pick_screenshot_device", ROOT / "ios" / "scripts" / "pick_screenshot_device.py")
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


def dev(name, udid, available=True):
    return {"name": name, "udid": udid, "isAvailable": available}


def payload(**runtimes):
    return {"devices": {k.replace("__", "."): v for k, v in runtimes.items()}}


IOS26 = "com.apple.CoreSimulator.SimRuntime.iOS-26-0"
IOS18 = "com.apple.CoreSimulator.SimRuntime.iOS-18-4"
TVOS = "com.apple.CoreSimulator.SimRuntime.tvOS-18-0"


class TestPicking:
    def test_picks_the_registered_phone(self):
        got = mod.pick({"devices": {IOS26: [
            dev("iPhone 16", "a"), dev("iPhone 16 Pro Max", "b")]}}, "APP_IPHONE_67")
        assert got[0] == "b" and got[1] == "iPhone 16 Pro Max"

    def test_candidate_order_beats_ios_version(self):
        """机型优先于系统版本。

        反过来（先取最新 iOS）会在镜像同时装了「新 iOS 的小屏机」和
        「旧 iOS 的 Pro Max」时挑错机型——而那正是尺寸不符的来源。
        """
        got = mod.pick({"devices": {
            IOS26: [dev("iPhone 15 Pro Max", "old-model-new-os")],
            IOS18: [dev("iPhone 17 Pro Max", "new-model-old-os")],
        }}, "APP_IPHONE_67")
        assert got[0] == "new-model-old-os"

    def test_newest_ios_within_the_same_model(self):
        got = mod.pick({"devices": {
            IOS18: [dev("iPhone 17 Pro Max", "old")],
            IOS26: [dev("iPhone 17 Pro Max", "new")],
        }}, "APP_IPHONE_67")
        assert got[0] == "new"

    def test_ipad_prefers_m5(self):
        """runner 镜像（2026-09）装的是 M5，ASC 上现有截图也来自 M5。

        第一版候选表只写到 M4，整个 iPad job 在挑机型那步就退出了。
        """
        got = mod.pick({"devices": {IOS26: [
            dev("iPad Pro 13-inch (M4)", "m4"), dev("iPad Pro 13-inch (M5)", "m5")]}},
            "APP_IPAD_PRO_3GEN_129")
        assert got[0] == "m5"

    def test_ipad_falls_back_when_m5_absent(self):
        got = mod.pick({"devices": {IOS26: [
            dev("iPad Pro 13-inch (M4)", "m4")]}}, "APP_IPAD_PRO_3GEN_129")
        assert got[0] == "m4"

    def test_ipad_does_not_accept_air_or_11_inch(self):
        """Air 和 11 寸的像素与 2064x2752 不同，挑中就是静默产出错尺寸。"""
        got = mod.pick({"devices": {IOS26: [
            dev("iPad Air 13-inch (M4)", "air"), dev("iPad Pro 11-inch (M5)", "p11"),
            dev("iPad (A16)", "base")]}}, "APP_IPAD_PRO_3GEN_129")
        assert got is None, f"不该挑中 {got}"

    def test_ipad_display_type(self):
        got = mod.pick({"devices": {IOS26: [
            dev("iPhone 17 Pro Max", "p"), dev("iPad Pro 13-inch (M5)", "t")]}},
            "APP_IPAD_PRO_3GEN_129")
        assert got[0] == "t"

    def test_name_match_tolerates_a_parenthesised_suffix(self):
        """镜像里机型名可能带后缀，匹配不能因此漏掉。"""
        got = mod.pick({"devices": {IOS26: [
            dev("iPad Pro 13-inch (M5) (2nd generation)", "x")]}},
            "APP_IPAD_PRO_3GEN_129")
        assert got[0] == "x"

    def test_pro_does_not_match_pro_max(self):
        """第一版用裸 startswith，这条直接失败。

        "iPhone 17 Pro Max".startswith("iPhone 17 Pro") 为真，于是挑 6.3" 的
        _61 会选到 Pro Max：产出 1320x2868 而不是 1206x2622，ASC 在上传那一步
        才拒绝，错误信息只说尺寸不符。
        """
        got = mod.pick({"devices": {IOS26: [
            dev("iPhone 17 Pro Max", "max")]}}, "APP_IPHONE_61")
        assert got is None, f"不该挑中 Pro Max，实际挑了 {got}"

    def test_pro_max_still_matches_its_own_type(self):
        got = mod.pick({"devices": {IOS26: [
            dev("iPhone 17 Pro Max", "max")]}}, "APP_IPHONE_67")
        assert got[0] == "max"

    def test_prefers_the_exact_pro_when_both_are_present(self):
        got = mod.pick({"devices": {IOS26: [
            dev("iPhone 17 Pro Max", "max"), dev("iPhone 17 Pro", "pro")]}},
            "APP_IPHONE_61")
        assert got[0] == "pro"


class TestExclusions:
    def test_ignores_unavailable_devices(self):
        """镜像里常留着 runtime 没下全的设备条目。选中它，xcodebuild 会
        在启动模拟器那一步才失败，错误信息很难读。"""
        got = mod.pick({"devices": {IOS26: [
            dev("iPhone 17 Pro Max", "a", available=False),
            dev("iPhone 16 Pro Max", "b")]}}, "APP_IPHONE_67")
        assert got[0] == "b"

    def test_ignores_non_ios_runtimes(self):
        got = mod.pick({"devices": {TVOS: [dev("iPhone 17 Pro Max", "a")]}},
                       "APP_IPHONE_67")
        assert got is None

    def test_unknown_display_type_returns_none(self):
        got = mod.pick({"devices": {IOS26: [dev("iPhone 17 Pro Max", "a")]}},
                       "APP_IPHONE_69")
        assert got is None

    def test_no_match_returns_none_rather_than_a_wrong_device(self):
        """挑不到宁可失败，也不能退回一台别的机型——那会静默产出错尺寸。"""
        got = mod.pick({"devices": {IOS26: [
            dev("iPhone 16", "a"), dev("iPhone SE (3rd generation)", "b")]}},
            "APP_IPHONE_67")
        assert got is None

    def test_empty_payload(self):
        assert mod.pick({}, "APP_IPHONE_67") is None
        assert mod.pick({"devices": {}}, "APP_IPHONE_67") is None


class TestCli:
    def test_exit_code_2_on_unknown_type(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["x", "NOPE"])
        assert mod.main() == 2

    def test_exit_code_1_when_nothing_matches(self, monkeypatch):
        import io as _io
        monkeypatch.setattr(sys, "argv", ["x", "APP_IPHONE_67"])
        monkeypatch.setattr(sys, "stdin", _io.StringIO('{"devices":{}}'))
        assert mod.main() == 1

    def test_prints_a_tab_separated_line(self, monkeypatch, capsys):
        import io as _io, json as _json
        monkeypatch.setattr(sys, "argv", ["x", "APP_IPHONE_67"])
        monkeypatch.setattr(sys, "stdin", _io.StringIO(_json.dumps(
            {"devices": {IOS26: [dev("iPhone 17 Pro Max", "udid-1")]}})))
        assert mod.main() == 0
        out = capsys.readouterr().out.strip()
        assert out.split("\t") == ["udid-1", "iPhone 17 Pro Max", "iOS 26.0"]


class TestRegistry:
    def test_every_registered_type_has_candidates(self):
        for dt, cands in mod.DEVICE_CANDIDATES.items():
            assert cands, dt
            assert all(isinstance(c, str) and c for c in cands), dt

    def test_covers_the_display_types_the_app_actually_uses(self):
        """ASC 上 FlatRadar 现有的 displayType 都必须登记。

        2026-09-03 实测：iPhone 那一档是 APP_IPHONE_61（1206x2622，6.3" Pro），
        不是 _67。写错的后果是产出一批 ASC 不接受的尺寸。
        """
        assert {"APP_IPHONE_61", "APP_IPAD_PRO_3GEN_129"} <= set(mod.DEVICE_CANDIDATES)

    def test_iphone_61_prefers_the_non_max_pro(self):
        """_61 是 6.3" 的 Pro。挑成 Pro Max 会得到 1320x2868，尺寸不符。"""
        assert mod.DEVICE_CANDIDATES["APP_IPHONE_61"][0] == "iPhone 17 Pro"
        assert all("Max" not in c for c in mod.DEVICE_CANDIDATES["APP_IPHONE_61"])
