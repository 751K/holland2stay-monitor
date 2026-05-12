# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Holland2Stay Monitor macOS .app
Build: pyinstaller --clean h2s_monitor.spec
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

_base = Path(SPECPATH).resolve().parent  # packaging/ → project root

# 自动收集 app/ 包下所有子模块作为 hiddenimports。
# 原因：web.py 用 ``from app.routes import (calendar_routes, control, ...)``
# 这种"包级批量导入子模块"的写法，部分 PyInstaller 版本的 modulegraph
# 静态分析可能漏掉个别成员；显式 collect_submodules 是零风险的兜底。
# 未来在 app/ 下新增模块也会被自动包含，不需要手动维护这份清单。
_app_modules = collect_submodules("app")

a = Analysis(
    [str(_base / "launcher.py")],
    pathex=[str(_base)],
    binaries=[],
    datas=[
        (str(_base / "templates"), "templates"),
        (str(_base / "static"), "static"),
        (str(_base / ".env.example"), "."),
    ],
    hiddenimports=[
        "curl_cffi",
        "cryptography",
        "cryptography.fernet",
        "cryptography.hazmat.backends",
        "cryptography.hazmat.primitives",
        "cryptography.hazmat.primitives.ciphers",
        "cryptography.hazmat.primitives.padding",
        "dotenv",
        "flask",
        "jinja2",
        *_app_modules,
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "numpy",
        "pandas",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="h2s-monitor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # 保留终端窗口，方便查看日志和 Ctrl+C 退出
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
