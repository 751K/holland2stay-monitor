# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Holland2Stay Monitor macOS .app
Build: pyinstaller --clean h2s_monitor.spec
"""

import sys
from pathlib import Path

_base = Path(SPECPATH).resolve()

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
