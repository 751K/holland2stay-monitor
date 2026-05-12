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
    def test_file_not_exists_returns_empty(self, tmp_path, monkeypatch):
        import users
        fake = tmp_path / "users.json"
        monkeypatch.setattr(users, "USERS_FILE", fake)
        result = users.load_users()
        assert result == []

    def test_empty_file_returns_empty(self, tmp_path, monkeypatch):
        import users
        fake = tmp_path / "users.json"
        fake.write_text("", encoding="utf-8")
        monkeypatch.setattr(users, "USERS_FILE", fake)
        # 空文件不是合法 JSON → 应抛 RuntimeError
        with pytest.raises(RuntimeError, match="解析失败"):
            users.load_users()

    def test_empty_list_returns_empty(self, tmp_path, monkeypatch):
        import users
        fake = tmp_path / "users.json"
        fake.write_text("[]", encoding="utf-8")
        monkeypatch.setattr(users, "USERS_FILE", fake)
        result = users.load_users()
        assert result == []

    def test_corrupted_json_raises(self, tmp_path, monkeypatch):
        import users
        fake = tmp_path / "users.json"
        fake.write_text("{broken json {{{", encoding="utf-8")
        monkeypatch.setattr(users, "USERS_FILE", fake)
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
        result = users.load_users()
        assert len(result) == 1
        assert result[0].name == "Test User"

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
        with caplog.at_level(logging.WARNING):
            result = users.load_users()
        assert len(result) == 1
        assert "未知字段" in caplog.text or any("unknown" in r.message.lower() for r in caplog.records)


class TestMigrateFromEnv:
    def test_no_env_config_returns_none(self, monkeypatch):
        monkeypatch.setenv("NOTIFICATION_CHANNELS", "")
        monkeypatch.setenv("IMESSAGE_RECIPIENT", "")
        from users import migrate_from_env
        assert migrate_from_env() is None

    def test_basic_migration_creates_user(self, monkeypatch):
        monkeypatch.setenv("NOTIFICATION_CHANNELS", "telegram")
        monkeypatch.setenv("IMESSAGE_RECIPIENT", "")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok123")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat456")
        from users import migrate_from_env
        u = migrate_from_env()
        assert u is not None
        assert u.name == "默认用户"
        assert "telegram" in u.notification_channels


class TestSaveUsers:
    def test_save_and_reload_round_trip(self, tmp_path, monkeypatch):
        import users
        fake = tmp_path / "users.json"
        monkeypatch.setattr(users, "USERS_FILE", fake)

        from users import UserConfig
        u = UserConfig(name="Test")
        users.save_users([u])
        assert fake.exists()
        loaded = users.load_users()
        assert len(loaded) == 1
        assert loaded[0].name == "Test"

    def test_save_empty_users_writes_empty_array(self, tmp_path, monkeypatch):
        import users
        fake = tmp_path / "users.json"
        monkeypatch.setattr(users, "USERS_FILE", fake)
        users.save_users([])
        assert json.loads(fake.read_text()) == []
