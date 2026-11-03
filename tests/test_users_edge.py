"""
users.py / SQLite user_configs 边缘恢复路径测试。

覆盖：
- 空库 → 直接从 SQLite user_configs 返回 []
- 保存 / 重新加载的往返

旧版 users.json 的导入已删除，启动检查见 tests/test_platform_cred_backfill.py。
"""
from __future__ import annotations



class TestLoadUsers:
    def test_file_not_exists_returns_empty(self, isolated_data_dir):
        import users
        result = users.load_users()
        assert result == []


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
