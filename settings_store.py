"""settings_store.py — runtime 类配置的取值顺序与一次性迁移

取值顺序
--------
::

    进程启动时的外部环境变量 > .env 中残留的运行参数 > app_settings > 代码默认值

运行时参数按来源解析后写入 ``os.environ``，供现有读取路径使用。真正的外部
覆盖在加载 .env 前捕获；重载只处理运行参数，不改变部署、密钥和调优配置。

为什么保留环境变量这一层
------------------------
容器化排障时需要一个不改数据库就能强制覆盖的口子（``docker compose run -e
CHECK_INTERVAL=30 ...``）。代价是它**看不见**：面板显示一个值，进程用的是另一个。
因此 ``source_of()`` 会如实报告每个键的来源，面板据此标注，
``env_registry.audit_keys()`` 也会对 .env 里残留的 runtime 键告警。

为什么迁移要把键从 .env 删掉
----------------------------
不删就会静默失效：``.env`` 被 ``load_dotenv()`` 加载进 ``os.environ``，于是它永远
赢过数据库，面板保存后毫无效果——按钮点了、提示成功了、什么都没发生。这正是
2026-08-05 那两个 bug 的形态（交接的两头都以为对方在负责）。所以迁移是**搬**而不是
**抄**：写进数据库之后从 .env 移除，移除前先整份备份。
"""

from __future__ import annotations

import logging
import contextvars
import uuid
from contextlib import contextmanager
import os
import re
import shutil
import time
import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from dotenv import dotenv_values
from dotenv.variables import parse_variables

from env_registry import DEPLOY_KEYS, RUNTIME_KEYS, SECRET_KEYS, TUNING_KEYS

logger = logging.getLogger(__name__)

#: 本进程从数据库注水进来的键。用于两件事：
#: ① 重新注水时可以覆盖自己上一次写的值（配置改了要生效），但仍不碰真实环境变量；
#: ② source_of() 据此区分「来自环境」和「来自数据库」——注水之后二者在
#:    os.environ 里长得一模一样，不记下来就分不出。
_hydrated: set[str] = set()


# Capture runtime/tuning values and only the *names* of secret/deploy overrides
# before loading .env. Never copy secrets into this provenance record.
_external_runtime: dict[str, str] | None = None
_external_fixed_keys: set[str] = set()
_runtime_lock = threading.RLock()
_active_runtime: "RuntimeCandidate | None" = None


@dataclass(frozen=True)
class RuntimeCandidate:
    """Read-only resolved runtime values and their source, prepared off-line."""
    values: Mapping[str, str]
    sources: Mapping[str, str]
    version: str = field(default_factory=lambda: uuid.uuid4().hex)
    saved_fingerprints: Mapping[str, str] | None = field(default=None, repr=False)
    dotenv_stamp: tuple[int, int] | None = None


@dataclass(frozen=True)
class RuntimeSnapshot:
    """One round’s environment and opaque, non-secret publication identifier."""
    version: str
    environment: Mapping[str, str] = field(repr=False)


_round_snapshot: contextvars.ContextVar[RuntimeSnapshot | None] = contextvars.ContextVar(
    "round_runtime_snapshot", default=None
)
_VERSION_KEY = "FLATRADAR_RUNTIME_VERSION"


def capture_runtime_snapshot() -> RuntimeSnapshot:
    current = _round_snapshot.get()
    if current is not None:
        return current
    ensure_runtime_active()
    with _runtime_lock:
        env = runtime_environment()
        version = _active_runtime.version
        env[_VERSION_KEY] = version
        return RuntimeSnapshot(version, MappingProxyType(env))


@contextmanager
def use_runtime_snapshot(snapshot: RuntimeSnapshot):
    token = _round_snapshot.set(snapshot)
    try:
        yield
    finally:
        _round_snapshot.reset(token)


def snapshot_from_environment(env: Mapping[str, str]) -> RuntimeSnapshot:
    return RuntimeSnapshot(env.get(_VERSION_KEY, "unversioned"), MappingProxyType(dict(env)))


def context_job(fn):
    """Capture the caller’s round context before queueing a thread job."""
    context = contextvars.copy_context()
    return lambda: context.run(fn)


def ensure_runtime_active() -> None:
    """Freeze current runtime settings even if startup hydration was unavailable."""
    global _active_runtime
    with _runtime_lock:
        if _active_runtime is None:
            values = {key: os.environ[key] for key in RUNTIME_KEYS if key in os.environ}
            sources = {key: "db" if key in _hydrated else "env" for key in values}
            _active_runtime = RuntimeCandidate(MappingProxyType(values), MappingProxyType(sources))


def runtime_get(key: str, default=None):
    snapshot = _round_snapshot.get()
    if snapshot is not None:
        return snapshot.environment.get(key, default)
    candidate = _active_runtime
    if candidate is not None and key in RUNTIME_KEYS:
        return candidate.values.get(key, default)
    return os.environ.get(key, default)


def runtime_environment(candidate: "RuntimeCandidate | None" = None) -> dict[str, str]:
    """Copy a coherent environment for parsing or passing to a worker."""
    snapshot = _round_snapshot.get()
    if candidate is None and snapshot is not None:
        return dict(snapshot.environment)
    with _runtime_lock:
        env = dict(os.environ)
        current = candidate if candidate is not None else _active_runtime
        if current is not None:
            for key in RUNTIME_KEYS:
                env.pop(key, None)
            env.update(current.values)
        return env


def commit_runtime(candidate: RuntimeCandidate, *, activate: bool = True) -> None:
    """Publish prepared values only after the caller has completed validation."""
    global _active_runtime
    if not candidate.values.keys() <= RUNTIME_KEYS:
        raise ValueError("候选配置包含非运行时参数")
    with _runtime_lock:
        old = {key: os.environ[key] for key in RUNTIME_KEYS if key in os.environ}
        try:
            for key in RUNTIME_KEYS - candidate.values.keys():
                os.environ.pop(key, None)
            for key, value in candidate.values.items():
                os.environ[key] = value
        except BaseException:
            for key in RUNTIME_KEYS - old.keys():
                os.environ.pop(key, None)
            for key, value in old.items():
                os.environ[key] = value
            raise
        _hydrated.clear()
        _hydrated.update(key for key, source in candidate.sources.items() if source == "db")
        if activate:
            _active_runtime = candidate


def capture_external_runtime() -> None:
    """Record genuine startup overrides before dotenv or database injection."""
    global _external_runtime
    if _external_runtime is None:
        _external_runtime = {key: os.environ[key] for key in RUNTIME_KEYS | TUNING_KEYS
                             if key in os.environ}
        _external_fixed_keys.update(key for key in SECRET_KEYS | DEPLOY_KEYS if key in os.environ)


def _runtime_dotenv(env_path: str | Path, stored: dict[str, str] | None = None) -> dict[str, str]:
    """Read only runtime entries; never log values or inject other categories."""
    raw = dotenv_values(dotenv_path=env_path, interpolate=False)
    # Do not interpolate against stale values injected by an earlier reload.
    # Genuine startup overrides still win, including inside ${...} references.
    base = runtime_environment()
    external = _external_runtime or {}
    for key in RUNTIME_KEYS:
        if key in external:
            base[key] = external[key]
        elif key not in raw and stored is not None and key in stored:
            base[key] = stored[key]
        else:
            base.pop(key, None)
    resolved: dict[str, str | None] = {}
    for key, value in raw.items():
        context = {**resolved, **base}
        resolved[key] = (None if value is None else
                         "".join(atom.resolve(context) for atom in parse_variables(value)))
    return {key: value for key, value in resolved.items()
            if key in RUNTIME_KEYS and value is not None}


def runtime_override_keys(env_path: str | Path) -> set[str]:
    """Return current external and .env overrides for the settings page."""
    external = set(_external_runtime or {}) & RUNTIME_KEYS
    try:
        raw = dotenv_values(dotenv_path=env_path, interpolate=False)
        return external | {key for key, value in raw.items()
                           if key in RUNTIME_KEYS and value is not None}
    except (OSError, UnicodeError):
        logger.warning("无法读取 .env 中的运行配置覆盖", exc_info=True)
        return external


def monitor_child_environment() -> dict[str, str]:
    """Do not promote Web's database/.env values to external monitor overrides."""
    env = dict(os.environ)
    if _external_runtime is None:
        return env
    for key in RUNTIME_KEYS | TUNING_KEYS:
        if key in _external_runtime:
            env[key] = _external_runtime[key]
        else:
            env.pop(key, None)
    for key in SECRET_KEYS | DEPLOY_KEYS:
        if key not in _external_fixed_keys:
            env.pop(key, None)
    return env


def prepare_runtime(storage, *, env_path: str | Path) -> RuntimeCandidate:
    """Resolve runtime sources without touching the active process environment."""
    from mcore.config_status import file_stamp
    stored = storage.all_app_settings()
    before_stamp = file_stamp(env_path)
    dotenv_runtime = _runtime_dotenv(env_path, stored)
    if file_stamp(env_path) != before_stamp:
        raise RuntimeError("读取配置时 .env 已更改，请重试")
    external = _external_runtime or {}
    values: dict[str, str] = {}
    sources: dict[str, str] = {}
    for key in RUNTIME_KEYS:
        if key in external:
            values[key], sources[key] = external[key], "env"
        elif key in dotenv_runtime:
            values[key], sources[key] = dotenv_runtime[key], "env"
        elif key in stored:
            values[key], sources[key] = stored[key], "db"
    for key in stored.keys() - RUNTIME_KEYS:
        logger.warning("app_settings 里的 %s 不属于 runtime 类，已忽略", key)
    from mcore.config_status import saved_fingerprints
    return RuntimeCandidate(MappingProxyType(values), MappingProxyType(sources),
                            saved_fingerprints=MappingProxyType(saved_fingerprints(stored)),
                            dotenv_stamp=before_stamp)


def hydrate(storage, *, env_path: str | Path, activate: bool = False) -> int:
    """Load runtime settings at startup; on read failure retain current values."""
    try:
        candidate = prepare_runtime(storage, env_path=env_path)
        commit_runtime(candidate, activate=activate)
    except Exception:
        logger.warning("读取运行配置失败，沿用当前运行配置", exc_info=True)
        return 0
    return sum(source == "db" for source in candidate.sources.values())


def source_of(key: str) -> str:
    """这个键当前的值从哪来：``env`` / ``db`` / ``default``。

    仅在 ``hydrate()`` 之后有意义。
    """
    if _active_runtime is not None and key in RUNTIME_KEYS:
        return _active_runtime.sources.get(key, "default")
    if key in _hydrated:
        return "db"
    return "env" if key in os.environ else "default"


def env_overrides(env_path: str | Path) -> list[str]:
    """.env 里还残留着哪些 runtime 键——它们会盖过面板。

    迁移之后正常应该是空的。非空说明有人手工加了回来，或迁移没跑成。
    """
    p = Path(env_path)
    if not p.exists():
        return []
    try:
        raw = dotenv_values(dotenv_path=p, interpolate=False)
        return [key for key, value in raw.items() if key in RUNTIME_KEYS and value is not None]
    except (OSError, UnicodeError):
        return []


def migrate_env_to_db(storage, env_path: str | Path) -> tuple[list[str], list[str]]:
    """一次性迁移：把 .env 里的 runtime 键搬进数据库，然后从 .env 移除。

    返回 ``(搬走的键, 跳过的键)``。跳过 = 数据库里已经有了，以数据库为准
    （重复运行不会用陈旧的 .env 值覆盖面板刚改过的配置）。

    幂等：搬完 .env 里就没有这些键了，再调一次是空操作。
    """
    p = Path(env_path)
    if not p.exists():
        return [], []

    present = env_overrides(p)
    if not present:
        return [], []

    try:
        existing = storage.all_app_settings()
    except Exception:
        logger.error("迁移前读取 app_settings 失败，本次不迁移", exc_info=True)
        return [], present

    raw = _runtime_dotenv(p, existing)
    moved: dict[str, str] = {}
    skipped: list[str] = []
    for key in present:
        if key in existing:
            skipped.append(key)
        else:
            moved[key] = raw.get(key, "")

    if moved:
        storage.set_app_settings(moved, updated_by="migration")

    backup = _backup_env(p)

    # 搬走的和跳过的都要从 .env 删掉：跳过的那些数据库里已有值，留在 .env
    # 只会继续盖住数据库，面板依旧改不动。
    _strip_keys(p, set(present))

    # 还得把它们从**内存里的** os.environ 撤掉。
    #
    # config 模块导入时就 load_dotenv() 了一遍，此刻这些键早已躺在 os.environ 里；
    # 只删文件的话，hydrate() 会认为「环境里已经有了，是外部强制指定」而跳过，
    # 于是首次升级后的这一程中面板怎么改都不生效——保存成功、毫无效果，
    # 又是一次静默失败。
    _forget_migrated(p, present, raw)

    logger.info(
        "配置迁移：%d 个键搬进 app_settings，%d 个以数据库为准；.env 已备份到 %s",
        len(moved), len(skipped), backup.name,
    )
    if moved:
        logger.info("  搬入: %s", ", ".join(sorted(moved)))
    if skipped:
        logger.info("  跳过（数据库已有值）: %s", ", ".join(sorted(skipped)))
    return sorted(moved), sorted(skipped)


def _backup_env(path: Path) -> Path:
    """动刀之前整份备份，返回备份路径。

    **备份落在 DATA_DIR，不落在 .env 旁边。** docker-compose 挂的是
    ``./.env:/app/.env``——**文件级** bind mount，只有这一个文件是共享的。写到
    ``/app/.env.bak.*`` 的东西留在容器自己的层里，``--force-recreate`` 一到就没了。
    2026-08-06 上线时实测：迁移正常完成，宿主机上却找不到任何备份。

    ``./data:/app/data`` 是目录挂载，才是真正持久的地方。写不进去时退回 .env
    旁边——总比没有强。
    """
    stamp = time.strftime("%Y%m%dT%H%M%S")
    try:
        from config import DATA_DIR

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        backup = Path(DATA_DIR) / f"env.bak.{stamp}"
        shutil.copy2(path, backup)
        return backup
    except Exception:
        logger.warning("备份到 DATA_DIR 失败，改写到 .env 旁边", exc_info=True)
        backup = path.with_name(f"{path.name}.bak.{stamp}")
        shutil.copy2(path, backup)
        return backup


def _forget_migrated(path: Path, keys: list[str], file_values: dict[str, str]) -> None:
    """把已搬走的键从 ``os.environ`` 里撤掉，好让 ``hydrate()`` 用数据库的值填回。

    只撤**值与 .env 文件一致**的那些。``load_dotenv()`` 默认不覆盖已存在的环境
    变量，因此若二者不同，说明有个真实的环境变量（docker-compose ``environment:``
    或 shell）正压着 .env 生效——那是有意的强制覆盖，不能动。

    config 加载 .env 前已记录真正的外部运行配置；即使值与文件相同也保留。
    """
    for key in keys:
        if key not in os.environ:
            continue
        if _external_runtime is not None:
            if key in _external_runtime:
                continue
            del os.environ[key]
            continue
        # Compatibility for callers that have not captured startup provenance.
        if os.environ[key] != file_values.get(key, ""):
            logger.info(
                "%s 有真实环境变量在覆盖 .env，保留该覆盖（面板对它无效）", key,
            )
            continue
        del os.environ[key]


def _strip_keys(path: Path, keys: set[str]) -> None:
    """从 .env 里删掉指定的键，**原地写回**。

    不能用临时文件 + ``os.replace()``：.env 是 Docker bind mount 的挂载点，
    原子 rename 会抛 ``OSError [Errno 16] Device or resource busy``。
    同一个坑 ``config.write_env_key()`` 也踩过。
    """
    kept = []
    for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if match and match.group(1) in keys:
            continue
        kept.append(line)
    path.write_text("".join(kept), encoding="utf-8")
