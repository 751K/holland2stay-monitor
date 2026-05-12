"""
i18n 翻译完整性测试。

覆盖：
- 所有 TRANSLATIONS key 有 zh 和 en
- tr() 对已知 key 返回非空
- tr() 对缺失 key 返回 key 本身（fallback）
- localize_options 返回格式正确
"""
from __future__ import annotations

import pytest
from translations import TRANSLATIONS, tr


class TestTranslationKeys:
    def test_all_keys_have_zh_and_en(self):
        for key, entry in TRANSLATIONS.items():
            assert "zh" in entry, f"Key '{key}' missing 'zh'"
            assert "en" in entry, f"Key '{key}' missing 'en'"
            assert isinstance(entry["zh"], (str, list)), f"Key '{key}' zh is not str/list"
            assert isinstance(entry["en"], (str, list)), f"Key '{key}' en is not str/list"

    def test_tr_returns_zh_for_known_key(self):
        assert tr("app_title", "zh") != "app_title"
        assert tr("app_title", "en") != "app_title"

    def test_tr_falls_back_to_key_for_missing(self):
        result = tr("nonexistent_key_xyz_123", "zh")
        assert result == "nonexistent_key_xyz_123"

    def test_tr_falls_back_to_zh_when_lang_missing(self):
        result = tr("app_title", "fr")  # French not supported
        assert result == TRANSLATIONS["app_title"]["zh"]


class TestLocalizeOptions:
    def test_returns_tuples(self, app_ctx):
        from app.i18n import localize_options
        result = localize_options("Occupancy", ["One", "Two"])
        assert len(result) == 2
        assert isinstance(result[0], tuple)
        assert len(result[0]) == 2  # (value, label)

    def test_unknown_value_falls_back_to_self(self, app_ctx):
        from app.i18n import localize_options
        result = localize_options("Occupancy", ["CustomUnknown"])
        assert result[0] == ("CustomUnknown", "CustomUnknown")
