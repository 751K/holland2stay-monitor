"""settings_store.py — runtime 类配置的取值顺序与一次性迁移

取值顺序
--------
::

    真实环境变量  >  app_settings 表  >  代码默认值

实现方式是**注水**：启动时把表里的值写进 ``os.environ``，但只写那些环境里还没有
的键。这样各模块几十处 ``os.environ.get(key, default)`` 一行都不用改，而顺序自动
成立——环境里已经有的说明是外部强制指定的，不动它。

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
import os
import shutil
import time
from pathlib import Path

from env_registry import RUNTIME_KEYS

logger = logging.getLogger(__name__)

#: 本进程从数据库注水进来的键。用于两件事：
#: ① 重新注水时可以覆盖自己上一次写的值（配置改了要生效），但仍不碰真实环境变量；
#: ② source_of() 据此区分「来自环境」和「来自数据库」——注水之后二者在
#:    os.environ 里长得一模一样，不记下来就分不出。
_hydrated: set[str] = set()


def hydrate(storage) -> int:
    """把 app_settings 表里的 runtime 配置注入 ``os.environ``。返回注入个数。

    幂等：重复调用会用表里的最新值刷新自己注过的键，但绝不覆盖真实环境变量。
    SIGHUP 热重载后必须再调一次——``load_dotenv(override=True)`` 会重放 .env，
    而配置早已不在 .env 里了。
    """
    try:
        stored = storage.all_app_settings()
    except Exception:
        # 配置读不出来不该让进程起不来：全部回落到代码默认值，照常跑。
        logger.warning("读取 app_settings 失败，本次全部使用代码默认值", exc_info=True)
        return 0

    n = 0
    for key, value in stored.items():
        if key not in RUNTIME_KEYS:
            # 表里混进了非 runtime 的键（手工 INSERT / 旧版本残留）。不注水——
            # 凭据注进环境变量会流进子进程和崩溃报告。
            logger.warning("app_settings 里的 %s 不属于 runtime 类，已忽略", key)
            continue
        if key in os.environ and key not in _hydrated:
            continue  # 外部强制指定，让它赢
        os.environ[key] = value
        _hydrated.add(key)
        n += 1
    return n


def source_of(key: str) -> str:
    """这个键当前的值从哪来：``env`` / ``db`` / ``default``。

    仅在 ``hydrate()`` 之后有意义。
    """
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
    found = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key = line.split("=", 1)[0].strip()
            if key in RUNTIME_KEYS:
                found.append(key)
    except OSError:
        return []
    return found


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

    raw = _read_env_pairs(p)
    moved: dict[str, str] = {}
    skipped: list[str] = []
    for key in present:
        if key in existing:
            skipped.append(key)
        else:
            moved[key] = raw.get(key, "")

    if moved:
        storage.set_app_settings(moved, updated_by="migration")

    # 整份备份再动刀。备份放在 .env 旁边而不是 /tmp——重启容器 /tmp 就没了，
    # 而出问题的人第一时间会去看 .env 所在的目录。
    backup = p.with_name(f"{p.name}.bak.{time.strftime('%Y%m%dT%H%M%S')}")
    shutil.copy2(p, backup)

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


def _forget_migrated(path: Path, keys: list[str], file_values: dict[str, str]) -> None:
    """把已搬走的键从 ``os.environ`` 里撤掉，好让 ``hydrate()`` 用数据库的值填回。

    只撤**值与 .env 文件一致**的那些。``load_dotenv()`` 默认不覆盖已存在的环境
    变量，因此若二者不同，说明有个真实的环境变量（docker-compose ``environment:``
    或 shell）正压着 .env 生效——那是有意的强制覆盖，不能动。

    残留的模糊地带：真实环境变量恰好与 .env 写的一样时，这里分辨不出来，会当成
    来自文件而撤掉。后果是该键此后由数据库接管；值不变，但后续改数据库会生效。
    要彻底分辨得在 ``load_dotenv()`` 之前抓一份原始环境，代价是 config 的导入
    顺序要跟着改，不值得。
    """
    for key in keys:
        if key not in os.environ:
            continue
        if os.environ[key] != file_values.get(key, ""):
            logger.info(
                "%s 有真实环境变量在覆盖 .env，保留该覆盖（面板对它无效）", key,
            )
            continue
        del os.environ[key]


def _read_env_pairs(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _strip_keys(path: Path, keys: set[str]) -> None:
    """从 .env 里删掉指定的键，**原地写回**。

    不能用临时文件 + ``os.replace()``：.env 是 Docker bind mount 的挂载点，
    原子 rename 会抛 ``OSError [Errno 16] Device or resource busy``。
    同一个坑 ``config.write_env_key()`` 也踩过。
    """
    kept = []
    for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            if stripped.split("=", 1)[0].strip() in keys:
                continue
        kept.append(line)
    path.write_text("".join(kept), encoding="utf-8")
