"""热重载分层守卫。

``RUNTIME_KEYS`` 是面板能改、且期望「保存即生效」的键。如果某个键在 import 期
被求值成模块级常量，面板改了它，进程里看到的仍是旧值。

本测试扫描模块级 / 类级赋值里的 env 读取，断言其中没有 RUNTIME_KEYS。
TUNING_KEYS 可以出现在这里——它们本来就不在面板上，改完要重启。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import env_registry as reg

ROOT = Path(__file__).resolve().parent.parent

_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")

#: 读取环境变量的函数名片段，与 tests/test_env_registry.py 保持同步。
_READER_NAMES = (
    "environ", "getenv", "_env_int", "_env_float", "_env_bool",
    "env_bool", "env_int", "_env_str", "_read", "_safe_int", "_safe_float",
)

_SKIP_DIRS = {"tests", ".venv", "__pycache__", "node_modules", "build", "dist", "tools"}


def _env_key_of(call: ast.Call) -> str | None:
    fn = call.func
    name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
    if not call.args:
        return None
    first = call.args[0]
    if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
        return None
    if isinstance(fn, ast.Attribute) and name in ("get", "getenv"):
        return first.value
    if any(r in name for r in _READER_NAMES):
        return first.value
    return None


def _module_level_env_keys() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}

    def note(key: str, path: Path) -> None:
        if _KEY_RE.match(key):
            found.setdefault(key, set()).add(str(path.relative_to(ROOT)))

    def scan(body, path: Path) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                scan(node.body, path)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = getattr(node, "value", None)
                if value is None:
                    continue
                for call in ast.walk(value):
                    if isinstance(call, ast.Call):
                        key = _env_key_of(call)
                        if key:
                            note(key, path)

    for path in sorted(ROOT.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        scan(tree.body, path)
    return found


def test_no_runtime_key_is_frozen_at_import():
    frozen = sorted(set(_module_level_env_keys()) & reg.RUNTIME_KEYS)
    assert not frozen, (
        f"这些 RUNTIME 键在 import 期被求值成常量，面板改了不会生效：{frozen}。"
        "改成函数式读取，或把它们移进 TUNING_KEYS 并接受需要重启"
    )


def test_every_panel_key_is_runtime():
    """面板能写的键必须都在 RUNTIME_KEYS 里，否则「保存即生效」是假的。"""
    from app.routes.settings import SETTINGS_KEYS

    extra = sorted(set(SETTINGS_KEYS) - reg.RUNTIME_KEYS)
    assert not extra, f"这些面板键不在 RUNTIME_KEYS 里，保存后不会热重载：{extra}"
