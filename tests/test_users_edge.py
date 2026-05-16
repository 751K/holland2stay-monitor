"""
users.py 边缘恢复路径测试。

覆盖：
- users.json 不存在 → 返回 []
- users.json 损坏 → 抛 RuntimeError
- 空文件 → 返回 []
- isolate env migration → create default user
"""
from __future__ import annotations

import json
import pytest


class TestLoadUsers:
    def test_file_not_exists_returns_empty(self, isolated_data_dir):
        import users
        result = users.load_users()
        assert result == []

    def test_empty_file_returns_empty(self, tmp_path, monkeypatch):
        import users
        fake = tmp_path / "users.json"
        fake.write_text("", encoding="utf-8")
        monkeypatch.setattr(users, "USERS_FILE", fake)
        monkeypatch.setattr("app.db.DB_PATH", tmp_path / "test.db")
        # 空文件不是合法 JSON → 应抛 RuntimeError
        with pytest.raises(RuntimeError, match="解析失败"):
            users.load_users()

    def test_empty_list_returns_empty(self, tmp_path, monkeypatch):
        import users
        fake = tmp_path / "users.json"
        fake.write_text("[]", encoding="utf-8")
        monkeypatch.setattr(users, "USERS_FILE", fake)
        monkeypatch.setattr("app.db.DB_PATH", tmp_path / "test.db")
        result = users.load_users()
        assert result == []
        assert list(tmp_path.glob("users.json.migrated.*.bak"))

    def test_corrupted_json_raises(self, tmp_path, monkeypatch):
        import users
        fake = tmp_path / "users.json"
        fake.write_text("{broken json {{{", encoding="utf-8")
        monkeypatch.setattr(users, "USERS_FILE", fake)
        monkeypatch.setattr("app.db.DB_PATH", tmp_path / "test.db")
        with pytest.raises(RuntimeError, match="解析失败"):
            users.load_users()

    def test_valid_users_loaded(self, tmp_path, monkeypatch):
        import users
        fake = tmp_path / "users.json"
        fake.write_text(json.dumps([{
            "name": "Test User",
            "id": "abc12345",
            "enabled": True,
            "notifications_enabled": True,
            "notification_channels": ["telegram"],
        }]), encoding="utf-8")
        monkeypatch.setattr(users, "USERS_FILE", fake)
        monkeypatch.setattr("app.db.DB_PATH", tmp_path / "test.db")
        result = users.load_users()
        assert len(result) == 1
        assert result[0].name == "Test User"
        assert result[0].id == "abc12345"
        assert list(tmp_path.glob("users.json.migrated.*.bak"))

    def test_unknown_fields_stripped_with_warning(self, tmp_path, monkeypatch, caplog):
        """旧版字段在加载时被剔除并 WARNING。"""
        import users, logging
        fake = tmp_path / "users.json"
        fake.write_text(json.dumps([{
            "name": "Old User",
            "id": "x1",
            "deleted_field": "should be stripped",
        }]), encoding="utf-8")
        monkeypatch.setattr(users, "USERS_FILE", fake)
        monkeypatch.setattr("app.db.DB_PATH", tmp_path / "test.db")
        with caplog.at_level(logging.WARNING):
            result = users.load_users()
        assert len(result) == 1
        assert "未知字段" in caplog.text or any("unknown" in r.message.lower() for r in caplog.records)

    def test_meta_flag_prevents_reimport(self, tmp_path, monkeypatch):
        import users
        fake = tmp_path / "users.json"
        fake.write_text(json.dumps([{"name": "Legacy", "id": "legacy01"}]), encoding="utf-8")
        monkeypatch.setattr(users, "USERS_FILE", fake)
        monkeypatch.setattr("app.db.DB_PATH", tmp_path / "test.db")

        first = users.load_users()
        assert [u.name for u in first] == ["Legacy"]

        fake.write_text(json.dumps([{"name": "Changed", "id": "changed1"}]), encoding="utf-8")
        second = users.load_users()
        assert [u.name for u in second] == ["Legacy"]


class TestSaveUsers:
    def test_save_and_reload_round_trip(self, isolated_data_dir):
        import users

        from users import UserConfig
        u = UserConfig(name="Test")
        users.save_users([u])
        loaded = users.load_users()
        assert len(loaded) == 1
        assert loaded[0].name == "Test"

    def test_save_empty_users_writes_empty_table(self, isolated_data_dir):
        import users
        users.save_users([])
        assert users.load_users() == []
