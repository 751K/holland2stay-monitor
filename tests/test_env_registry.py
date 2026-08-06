""".env 的键名必须三方对齐：代码读的、registry 登记的、example 记录的。

配置散在几十个文件的 ``os.environ.get()`` 里，此前没有任何地方能回答「一共有哪些
键」。后果是键名打错**完全静默**——``PEAK_STRAT=08:30`` 不报错，只是安静地走默认
值，而你以为自己改了。

守三条边：

    代码实际读取  ←→  env_registry  ←→  .env.example

任意一条断了都有具体后果：

- 代码读了但 registry 没登记 → 启动审计会把它报成「不认识的键」，
  用户照着提示删掉，配置静默失效
- registry 登记了但代码不读 → 死键，误导后来者以为它有用
  （NOTIFICATIONS_ENABLED 就是这么在生产 .env 里躺了很久的）
- 代码读了但 example 没记录 → 部署的人根本不知道有这个开关

扫描器的已知边界
----------------
它按**当前代码里存在的读取形态**匹配：``os.environ.get`` / ``os.environ[...]`` /
``_env_int`` / ``_env_float`` / ``_read``，以及 ``*_ENV`` 常量持有键名。如果将来有
人用新的包装函数读环境变量，扫描器看不见它——那时这个测试会**静默漏掉**，而不是
报错。新增包装函数请同时把名字加进 ``_READER_NAMES``。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import env_registry as reg

ROOT = Path(__file__).resolve().parent.parent

#: 读取环境变量的函数名片段。见模块 docstring 里的「已知边界」。
_READER_NAMES = ("environ", "getenv", "_env_int", "_env_float", "_env_bool", "_env_str", "_read")

#: 环境变量键名的形状。三字符起步，避免把 "ID" "OK" 这类常量当成配置键。
_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")

#: tools/ 是独立的开发脚本（App Store Connect 上传、clearance 探针等），不随应用
#: 部署，也不该出现在部署文档里。ASC_CONFIG 只服务于 tools/asc。
_SKIP_DIRS = {"tests", ".venv", "__pycache__", "node_modules", "build", "dist", "tools"}


def _scan_source_keys() -> dict[str, set[str]]:
    """扫出代码里实际读取的 env 键 → 出现在哪些文件。"""
    found: dict[str, set[str]] = {}

    def note(name: str, path: Path) -> None:
        if _KEY_RE.match(name):
            found.setdefault(name, set()).add(str(path.relative_to(ROOT)))

    for path in sorted(ROOT.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and node.args:
                fn = ast.unparse(node.func)
                first = node.args[0]
                if any(r in fn for r in _READER_NAMES) and isinstance(first, ast.Constant):
                    if isinstance(first.value, str):
                        note(first.value, path)
            elif isinstance(node, ast.Subscript) and "environ" in ast.unparse(node.value):
                if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                    note(node.slice.value, path)
            elif isinstance(node, ast.Assign):
                # _CAPTURE_PATH_ENV = "OURCAMPUS_CAPTURE_PATH" 这类间接持有
                for t in node.targets:
                    if not isinstance(t, ast.Name):
                        continue
                    if not t.id.rstrip("_").upper().endswith(("_ENV", "_ENV_KEY", "_ENV_VAR")):
                        continue
                    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                        note(node.value.value, path)
    return found


def _example_keys() -> set[str]:
    """.env.example 里记录的键（含注释掉的可选项）。"""
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    return set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]{2,})=", text, flags=re.MULTILINE))


class TestRegistryMatchesCode:
    def test_no_unregistered_key(self):
        missing = sorted(set(_scan_source_keys()) - reg.KNOWN_ENV_KEYS)
        assert not missing, (
            f"这些键代码在读，env_registry 却没登记：{missing}。"
            "启动审计会把它们报成「不认识的键」，照提示删掉就静默失效了"
        )

    def test_no_phantom_key(self):
        stale = sorted(reg.KNOWN_ENV_KEYS - set(_scan_source_keys()))
        assert not stale, (
            f"这些键登记了但代码从不读：{stale}。"
            "要么是删代码时漏了，要么该进 RETIRED_KEYS 并写明为什么"
        )

    def test_every_key_has_exactly_one_tier(self):
        """类别决定这个键该住在哪儿，重复归类等于没归类。"""
        for key in sorted(reg.KNOWN_ENV_KEYS):
            tiers = [t for t, keys in reg.TIERS.items() if key in keys]
            assert len(tiers) == 1, f"{key} 归了 {len(tiers)} 个类：{tiers}"

    def test_tier_of_agrees_with_the_tables(self):
        for tier, keys in reg.TIERS.items():
            for key in keys:
                assert reg.tier_of(key) == tier
        assert reg.tier_of("ZZZ_NOT_A_KEY") is None


class TestExampleDocumentsEverything:
    def test_every_known_key_appears(self):
        missing = sorted(reg.KNOWN_ENV_KEYS - _example_keys())
        assert not missing, (
            f".env.example 没记录这些键：{missing}。部署的人不会知道它们存在"
        )

    def test_example_has_no_unknown_key(self):
        extra = sorted(_example_keys() - reg.KNOWN_ENV_KEYS)
        assert not extra, f".env.example 记录了不存在的键：{extra}"

    def test_retired_keys_are_not_advertised(self):
        """废弃的键不能还留在示例里，否则新部署会照抄一遍。"""
        for key in reg.RETIRED_KEYS:
            assert key not in _example_keys(), f"{key} 已废弃，不该出现在 .env.example"


class TestSecretsStayOutOfTheDatabase:
    """阶段二会把 runtime 类搬进 SQLite。这条边界要在搬之前就立住。

    库会被备份、导出、下载——凭据一旦进去就跟着走。
    """

    @pytest.mark.parametrize("key", sorted(reg.SECRET_KEYS))
    def test_secret_is_not_also_runtime(self, key):
        assert key not in reg.RUNTIME_KEYS, f"{key} 是凭据，不能进迁移名单"

    def test_proxy_urls_are_secrets(self):
        """代理 URL 形如 http://user:pass@host:port，凭据就在字符串里。"""
        for key in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "SCRAPE_PROXIES_FALLBACK"):
            assert reg.tier_of(key) == "secret"

    def test_bootstrap_keys_stay_in_env(self):
        """DB_PATH 必须在读数据库之前就可用——它没法从数据库里读。"""
        assert reg.tier_of("DB_PATH") == "deploy"


class TestPanelWrittenKeysAreRuntime:
    """面板正在写的键必须全部归在 runtime——那正是这一类的定义。

    漏一个，阶段二迁移就会剩一个还在写 .env 的路径，锁和 bind-mount 变通也就退不掉。
    """

    def test_settings_page_keys(self):
        from app.routes.settings import SETTINGS_KEYS

        for key in SETTINGS_KEYS:
            assert reg.tier_of(key) == "runtime", f"{key} 被 /settings 写，却不在 runtime"

    def test_scope_keys_written_by_the_same_page(self):
        """SOURCES / CITIES 那几个在 settings.py 里单独 write_env_key，不在 SETTINGS_KEYS。"""
        src = (ROOT / "app" / "routes" / "settings.py").read_text(encoding="utf-8")
        for key in re.findall(r'write_env_key\(\s*"([A-Z_]+)"', src):
            assert reg.tier_of(key) == "runtime", f"{key} 被面板写，却不在 runtime"


class TestAudit:
    def test_typo_gets_a_suggestion(self):
        [msg] = reg.audit_keys(["PEAK_STRAT"])
        assert "PEAK_START" in msg

    def test_known_keys_are_silent(self):
        assert reg.audit_keys(sorted(reg.KNOWN_ENV_KEYS)) == []

    def test_docker_injected_keys_are_silent(self):
        """NO_PROXY 由 docker-compose 显式注入（避免代理拦 localhost 健康检查）。"""
        assert reg.audit_keys(["NO_PROXY", "PATH", "TZ"]) == []

    def test_retired_key_says_why(self):
        [msg] = reg.audit_keys(["NOTIFICATIONS_ENABLED"])
        assert "已废弃" in msg
        assert "user_configs" in msg, "只说「未知」的话，看到的人会以为是打错了又加回来"

    def test_unknown_key_without_a_close_match(self):
        [msg] = reg.audit_keys(["ZZZ_TOTALLY_UNRELATED"])
        assert "不会有任何效果" in msg

    def test_audit_file_parses_real_syntax(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text(
            "# 注释\n"
            "\n"
            "WEB_PASSWORD=hunter2\n"
            "  PEAK_STRAT = 08:30\n"          # 两侧空格 + 打错
            "PUBLIC_BASE_URL=https://x/a=b\n"  # 值里带 =
            "没有等号的一行\n",
            encoding="utf-8",
        )
        warnings = reg.audit_env_file(env)
        assert len(warnings) == 1 and "PEAK_START" in warnings[0]

    def test_missing_file_is_not_an_error(self, tmp_path):
        assert reg.audit_env_file(tmp_path / "nope.env") == []

    def test_startup_hook_logs_and_counts(self, tmp_path, caplog):
        env = tmp_path / ".env"
        env.write_text("ZZZ_NONSENSE=1\n", encoding="utf-8")
        with caplog.at_level("WARNING"):
            assert reg.log_env_audit(env) == 1
        assert any("ZZZ_NONSENSE" in r.getMessage() for r in caplog.records)


class TestMonitorRunsTheAudit:
    """审计接在启动路径上，否则它只是一个没人调的纯函数。"""

    def test_wired_into_startup(self):
        src = (ROOT / "monitor.py").read_text(encoding="utf-8")
        assert "log_env_audit(ENV_PATH)" in src

    def test_does_not_block_startup(self):
        """一个拼错的键让整个监控起不来，代价远大于它本身。"""
        import inspect

        assert "raise" not in inspect.getsource(reg.log_env_audit)
