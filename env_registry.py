"""env_registry.py — .env 里认得哪些键，各属于哪一类

为什么需要它
------------
配置散在 71 个文件的 ``os.environ.get()`` 调用里，没有任何地方能回答两个很基本的
问题：**一共有哪些键**，以及**哪些是换机器才改、哪些是天天改**。后果有两个：

- 键名打错是**完全静默**的。``PEAK_STRAT=08:30`` 不会报错，只会安静地走默认值，
  而你以为自己改了。
- 无从判断一个键该放哪儿。用户级配置早就搬进了 SQLite，系统级这半却始终堆在
  同一个文件里——凭据、部署事实、面板每天在改的运维项、几乎没人动的阈值，
  生命周期完全不同的四种东西共用一份文件和一套读写方式。

本模块只做一件事：把键名和它的**类别**记下来。不存默认值——那是各模块
``os.environ.get(key, default)`` 的事，抄一份到这里只会产生第二个真相来源。

类别的含义
----------
类别不是分类学练习，它回答的是「这个键该住在哪里」：

``secret``
    凭据，或指向凭据材料的路径。**永远留在环境变量里。** 不进数据库（库会被
    备份、导出、下载），不进日志。
``deploy``
    部署事实：路径、时区、对外基址、是否 HTTPS。换一台机器或换一个域名才改，
    而且必须在**读数据库之前**就可用——``DB_PATH`` 是典型，没有它连设置表都找不到。
    因此它们同样必须留在环境变量里。
``runtime``
    运维调参与监控范围。**Web 面板正在运行时改写它们**（见
    ``app/routes/settings.py:SETTINGS_KEYS``），这也是 ``app/env_writer.py`` 那把锁
    与「不能用 ``os.replace()``，会断 Docker bind mount」那个变通存在的唯一原因——
    一个本该只读的部署产物为了被运行时写，绕了两层。这一类是搬进 SQLite 的目标。
``tuning``
    行为阈值与开关，全都有代码默认值，绝大多数部署一个都不用填。留在这里不碍事，
    但不该和上面三类混在一起呈现给第一次部署的人看。

维护
----
新增一个读 ``os.environ`` 的键就要在这里登记一个，否则
``tests/test_env_registry.py`` 会失败并告诉你漏了哪个。这是有意的摩擦：一个键
悄悄出现、既没文档也没人知道它存在，正是当初的状态。
"""
from __future__ import annotations

import difflib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

#: 凭据。留在环境变量，不进数据库、不进日志、不进备份。
SECRET_KEYS: frozenset[str] = frozenset({
    "WEB_USERNAME", "WEB_PASSWORD",
    "FLASK_SECRET", "DATA_ENCRYPTION_KEY",
    "RESEND_API_KEY", "RESEND_WEBHOOK_SECRET",
    # 路径本身不是密钥，但指向密钥材料，泄露路径等于给出攻击目标
    "APNS_KEY_PATH", "APNS_KEY_ID", "APNS_TEAM_ID",
    "FCM_SERVICE_ACCOUNT_PATH", "FCM_PRIVATE_KEY",
    "FCM_CLIENT_EMAIL", "FCM_PROJECT_ID",
    "GOOGLE_MAPS_API_KEY", "CAPTCHA_API_KEY",
    # 代理 URL 形如 http://user:pass@host:port——凭据就在字符串里
    "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "SCRAPE_PROXIES_FALLBACK",
    # 个人线路同样是完整代理 URL，可能带凭据
    "SCRAPE_PROXIES_PERSONAL",
})

#: 部署事实。换机器/换域名才改；且部分在能读数据库之前就要可用。
DEPLOY_KEYS: frozenset[str] = frozenset({
    "DB_PATH", "DATA_DIR",
    "TIMEZONE", "PUBLIC_BASE_URL", "SUPPORT_EMAIL",
    "GOOGLE_SITE_VERIFICATION",
    "SESSION_COOKIE_SECURE",
    "FLASK_DEBUG", "SUPERVISOR_CONF",
    "CLOAKBROWSER_HEADLESS",
})

#: 运维调参与监控范围。Web 面板运行时在改写，是搬进 SQLite 的目标。
RUNTIME_KEYS: frozenset[str] = frozenset({
    # 轮询节奏（/settings 页直接可改）
    "CHECK_INTERVAL", "LOG_LEVEL",
    "PEAK_INTERVAL", "MIN_INTERVAL",
    "PEAK_START", "PEAK_END", "PEAK_START_2", "PEAK_END_2",
    "PEAK_WEEKDAYS_ONLY", "JITTER_RATIO",
    "HEARTBEAT_INTERVAL_MINUTES",
    # 监控范围（同样由面板写回）
    "SOURCES", "SHADOW_SOURCES",
    "CITIES", "OURDOMAIN_CITIES", "OURCAMPUS_CITIES", "XIOR_CITIES",
    "AVAILABILITY_FILTERS",
    "SHARD_SIZES", "SOURCE_MIN_INTERVALS", "SOURCE_PEAK_MIN_INTERVALS",
})

#: 行为阈值与开关。都有代码默认值，绝大多数部署一个都不必填。
TUNING_KEYS: frozenset[str] = frozenset({
    # 通知渠道开关与配额
    "SHARED_EMAIL_ENABLED", "RESEND_FROM",
    "RESEND_GLOBAL_DAILY_LIMIT", "RESEND_PER_USER_DAILY_LIMIT",
    "INBOUND_FORWARD_TO",
    "APNS_ENABLED", "APNS_TOPIC", "APNS_ENV_DEFAULT", "APNS_CONCURRENCY",
    "FCM_ENABLED", "FCM_CONCURRENCY", "FCM_REQUEST_TIMEOUT",
    # Web
    "WEB_GUEST_MODE", "SESSION_LIFETIME_HOURS",
    # 抓取传输层
    "BROWSER_PERSIST_PROFILE", "BROWSER_BLOCK_RESOURCES",
    "BROWSER_BYTE_ACCOUNTING",
    "OURDOMAIN_IMPERSONATES", "OURDOMAIN_WAF_RETRIES",
    "OURDOMAIN_ZERO_ROUNDS_TO_CONFIRM", "OURCAMPUS_CAPTURE_PATH",
    # 健康与告警判据
    "MONITOR_HEARTBEAT_MAX_AGE",
    "HEALTH_WINDOW_ROUNDS", "HEALTH_FAIL_STREAK_DOWN",
    "HEALTH_ZERO_STREAK_WARN", "HEALTH_STALE_FULL_SCAN_SECONDS",
    "HEALTH_SILENT_SECONDS",
    "WATCHDOG_REPEAT_INTERVAL",
    # 走自家线路时的主动降速下限（见 config.is_personal_proxy_active）
    "PERSONAL_PROXY_MIN_INTERVAL",
    # 账户级代理故障（402 欠费 / 407 认证失败）的冷却时长
    "PROXY_ACCOUNT_COOLDOWN_SEC",
    # 状态收敛与预订
    "STALE_RESERVED_HOURS", "STALE_OCCUPIED_HOURS",
    "BOOKING_STATUS_HOLD_MINUTES",
})

TIERS: dict[str, frozenset[str]] = {
    "secret": SECRET_KEYS,
    "deploy": DEPLOY_KEYS,
    "runtime": RUNTIME_KEYS,
    "tuning": TUNING_KEYS,
}

KNOWN_ENV_KEYS: frozenset[str] = frozenset().union(*TIERS.values())

#: 由 Docker / compose / 系统注入，不是本项目的配置，出现在 .env 里也不该告警。
#: NO_PROXY 尤其重要：docker-compose.yml 显式设它来避免代理拦截 localhost 健康检查。
_EXTERNAL_KEYS: frozenset[str] = frozenset({
    "NO_PROXY", "no_proxy", "PATH", "HOME", "LANG", "LC_ALL", "TZ",
    "PYTHONPATH", "PYTHONUNBUFFERED", "PORT", "GUNICORN_CMD_ARGS",
})

#: 已废弃的键：曾经有效，现在读了也没人用。单独列出来是为了给出**为什么**——
#: 只说「未知的键」会让人以为是打错了，于是又把它加回去。
RETIRED_KEYS: dict[str, str] = {
    "NOTIFICATIONS_ENABLED": (
        "这是用户级开关，早已随通知设置一起迁到 SQLite user_configs；"
        ".env 里的同名键从未被读取。同名字符串在代码里只作为 HTML 表单字段名出现。"
    ),
}


def tier_of(key: str) -> str | None:
    """返回键所属类别；不认识则 None。"""
    for tier, keys in TIERS.items():
        if key in keys:
            return tier
    return None


def suggest(key: str) -> str | None:
    """猜一个最接近的已知键名，用于「是不是打错了」。"""
    match = difflib.get_close_matches(key, sorted(KNOWN_ENV_KEYS), n=1, cutoff=0.7)
    return match[0] if match else None


def audit_keys(present: list[str]) -> list[str]:
    """检查一批键名，返回可直接写进日志的告警文案（无问题则空列表）。

    只看**调用方给的键名**，不看 ``os.environ``——后者混着一大堆系统变量，
    逐个判定只会刷屏。调用方应传入 .env 文件里实际写了的那些键。
    """
    warnings: list[str] = []
    for key in present:
        if key in KNOWN_ENV_KEYS or key in _EXTERNAL_KEYS:
            continue
        if key in RETIRED_KEYS:
            warnings.append(f"{key} 已废弃，可以从 .env 删掉：{RETIRED_KEYS[key]}")
            continue
        hint = suggest(key)
        warnings.append(
            f"{key} 不是本项目认识的配置键，它不会有任何效果"
            + (f"（是不是想写 {hint}？）" if hint else "")
        )
    return warnings


def audit_env_file(path: str | Path) -> list[str]:
    """读 .env 文件并审计其中的键名。文件不存在时返回空列表。"""
    p = Path(path)
    if not p.exists():
        return []
    keys: list[str] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            keys.append(line.split("=", 1)[0].strip())
    except OSError as e:
        logger.debug("读取 .env 失败，跳过键名审计: %s", e)
        return []
    return audit_keys(keys)


def log_env_audit(path: str | Path) -> int:
    """启动时调一次：把审计结果写进日志。返回告警条数。

    只 WARNING 不阻断启动——一个拼错的键让整个监控起不来，代价远大于它本身。
    """
    warnings = audit_env_file(path)
    for w in warnings:
        logger.warning("⚙️  .env: %s", w)
    return len(warnings)
