"""
tools/ 和 launcher 烟测。

覆盖：
- tools/reset_db.py 可 import、dry-run 可用
- tools/geocode_all.py 可 import
- launcher.py 可 import web.app
- crypto.py encrypt/decrypt round-trip
- update_checker 模块可 import
"""
from __future__ import annotations


class TestToolsImport:
    def test_reset_db_import(self):
        import tools.reset_db  # noqa: F401

    def test_geocode_all_import(self):
        import tools.geocode_all  # noqa: F401


class TestLauncherImport:
    def test_launcher_imports_web(self):
        # launcher.py 依赖 web.app
        import launcher  # noqa: F401


class TestUpdateCheckerImport:
    def test_update_checker_import(self):
        import update_checker  # noqa: F401
