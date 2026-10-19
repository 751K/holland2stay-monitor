"""启动与热重载：日志、配置注水、配置自检、APNs 诊断。

从 monitor.py 拆出。日志通道仍用 ``monitor``——面板、告警和测试都按这个
logger 名字过滤，改名属于另一个变更。
"""
from __future__ import annotations

import logging

from config import DATA_DIR, ENV_PATH
from users import UserConfig

logger = logging.getLogger("monitor")


def setup_logging(level: str) -> None:
    """
    配置主日志（monitor.log，全量 INFO+）+ 错误日志（errors.log，仅 WARNING+）。

    错误日志的存在意义
    ------------------
    monitor.log 长跑下 INFO 噪音淹没真正的告警；errors.log 单独保留
    WARNING/ERROR/CRITICAL，便于事后排查抓取失败、下单异常、限流等问题。
    - 更大的 backupCount：错误稀疏，保留更长时间窗口（10MB 历史）
    - 更详细的 formatter：含 funcName:lineno，一眼定位问题源
    - 全局 root logger 接管：所有模块的 logger.warning/error 自动入此文件

    **可重入。** 启动时会调两次：第一次用环境里的 LOG_LEVEL 起个头，好让
    bootstrap_settings() 里的迁移日志有地方去；注水之后再按最终配置调一次。
    每次先摘掉自己上一轮装的 handler，否则每行日志会被写两遍。
    """
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    root = logging.getLogger()
    for h in [h for h in root.handlers if getattr(h, "_monitor_owned", False)]:
        root.removeHandler(h)
        h.close()
    logging.basicConfig(level=getattr(logging, level, "INFO"), format=fmt)
    root.setLevel(getattr(logging, level, logging.INFO))
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    from logging.handlers import RotatingFileHandler
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # 主日志（INFO+）：与之前一致，Web 面板默认查看。
    # 100MB：2MB 时一个文件只装得下一天左右，回头查几天前的事只能翻 .1/.2/.3。
    # 读它的地方都是有界的——/api/logs 只读尾部（上限 64MB），仪表盘
    # _avg_run_count 从尾部倒着读到 7 天前为止——不会因为文件变大而整份读入内存。
    main_fh = RotatingFileHandler(
        str(DATA_DIR / "monitor.log"),
        maxBytes=100 * 1024 * 1024, backupCount=3, encoding="utf-8",
    )
    main_fh.setFormatter(logging.Formatter(fmt))
    main_fh.setLevel(getattr(logging, level, "INFO"))
    main_fh._monitor_owned = True
    logging.getLogger().addHandler(main_fh)

    # 错误日志（WARNING+）：抓取/下单异常的专用归档
    error_fh = RotatingFileHandler(
        str(DATA_DIR / "errors.log"),
        maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8",
    )
    error_fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s.%(funcName)s:%(lineno)d %(message)s"
    ))
    error_fh.setLevel(logging.WARNING)
    error_fh._monitor_owned = True
    logging.getLogger().addHandler(error_fh)


def validate_structured_config() -> None:
    """校验监控范围那批「塞在字符串里的表格」，把问题吼出来。

    面板保存时已经挡过一道，但值还能从别处来：手工改库、迁移、环境变量覆盖、
    从旧版本升上来。这里是最后一道。

    **不阻断启动。** 一个配置笔误让整个监控停摆，代价远大于笔误本身；何况多数
    问题只影响一个平台，其余照跑更有价值。但要打 ERROR，让它进 errors.log 和
    /monitoring 面板。
    """
    try:
        from target_config import validate_effective

        problems = validate_effective()
    except Exception:
        logger.debug("结构化配置自检失败（已忽略）", exc_info=True)
        return

    for p in problems:
        if p.fatal:
            logger.error("⚙️  配置格式有误：%s", p)
        else:
            logger.warning("⚙️  配置提示：%s", p)


def check_registry_drift(storage) -> None:
    """注册表加了城市、订阅串没跟上——启动时报出来。

    见 target_config 里「注册表漂移」那段：这是 2026-09-08 Plaza / Rijswijk 那次
    的第三个副本。加城市只是让它出现在设置页上，不勾就不会抓，而 scraper 那条
    「未登记城市」的 WARNING 恰恰会因为注册表有了它而闭嘴。

    整段吞异常：这只是一条提醒，读不出快照不该让 monitor 起不来。

    快照坏了（手工改库、写到一半断电）按「第一次运行」处理，本轮不报、顺手写一份
    好的回去。**这一段不能和下面共用一个 except**：读快照抛异常时如果连
    ``set_meta`` 一起跳过，坏值就永远留在那儿，这条检查从此静默失效——正是它要防
    的那种失败。
    """
    import json
    import os

    from target_config import REGISTRY_SEEN_META_KEY, registry_drift

    seen: dict | None = None
    try:
        raw = storage.get_meta(REGISTRY_SEEN_META_KEY, "")
        if raw and raw != "—":
            seen = json.loads(raw)
        if not isinstance(seen, dict):
            seen = None
    except Exception:
        logger.warning("注册表快照读不出来（本轮按首次运行处理，将重写一份）",
                       exc_info=True)
        seen = None

    try:
        problems, snapshot = registry_drift(dict(os.environ), seen)
        for p in problems:
            logger.warning("⚙️  配置提示：%s", p)
        storage.set_meta(REGISTRY_SEEN_META_KEY,
                         json.dumps(snapshot, sort_keys=True))
    except Exception:
        logger.debug("注册表漂移自检失败（已忽略）", exc_info=True)


def bootstrap_settings() -> None:
    """把 runtime 类配置从 app_settings 表注入 os.environ；首次运行顺带做迁移。

    必须在 ``load_config()`` 之前调用——后者读的就是 ``os.environ``。

    整段吞异常：配置存储出问题时全部回落到 .env / 代码默认值继续跑。让监控因为
    读不出配置而起不来，代价远大于用一轮旧配置。
    """
    try:
        from config import DB_PATH, TIMEZONE
        from settings_store import env_overrides, hydrate, migrate_env_to_db
        from storage import Storage

        st = Storage(DB_PATH, timezone_str=TIMEZONE)
        try:
            migrate_env_to_db(st, ENV_PATH)
            n = hydrate(st)
            if n:
                logger.info("已从 app_settings 载入 %d 项运维配置", n)
            # 迁移之后 .env 里不该再有 runtime 键。有就是被手工加回来了——它会
            # 盖过面板，而面板不会有任何提示，改了没反应会让人以为是坏了。
            leftover = env_overrides(ENV_PATH)
            if leftover:
                logger.warning(
                    "⚙️  .env 里这些键会盖过面板设置（面板改了不生效）：%s。"
                    "确认要用面板管理的话，从 .env 删掉即可",
                    ", ".join(leftover),
                )
            validate_structured_config()
            check_registry_drift(st)
        finally:
            st.close()

    except Exception:
        logger.warning("载入 app_settings 失败，本次使用 .env / 默认值", exc_info=True)


def reload_settings() -> None:
    """热重载时重新注水。

    ``load_dotenv(override=True)`` 会把 .env 重放一遍，而 runtime 配置早已不在
    .env 里；不重新注水的话，面板刚改的值这一程都不会生效。
    """
    try:
        from config import DB_PATH, TIMEZONE
        from settings_store import hydrate
        from storage import Storage

        st = Storage(DB_PATH, timezone_str=TIMEZONE)
        try:
            hydrate(st)
        finally:
            st.close()
    except Exception:
        logger.warning("热重载时读取 app_settings 失败，沿用当前值", exc_info=True)


def apns_startup_diag(st, users: list[UserConfig]) -> None:
    """启动时诊断 APNs 设备关联，发现配置问题尽早 WARNING。"""
    from mcore import push as _push

    if _push.get_client() is None:
        logger.warning("APNs 未启用：启动时无法获取 ApnsClient（检查 APNS_ENABLED / .p8 / APNS_*）")
        return

    # 统计全局数据
    try:
        total_devs = st.conn.execute(
            "SELECT COUNT(*) FROM device_tokens WHERE disabled_at IS NULL"
        ).fetchone()
        total_tokens = st.conn.execute(
            "SELECT COUNT(*) FROM app_tokens WHERE revoked = 0"
        ).fetchone()
        logger.info(
            "APNs 启动诊断：DB 中 %d 个活跃设备，%d 个活跃 token",
            total_devs[0] if total_devs else 0,
            total_tokens[0] if total_tokens else 0,
        )
    except Exception:
        logger.exception("APNs 启动诊断：查询设备/ token 计数失败")
        return

    # admin 设备诊断
    try:
        admin_devs = st.get_active_devices_for_admin()
        if admin_devs:
            logger.info("APNs admin: %d 个可推送设备", len(admin_devs))
        else:
            admin_tokens = st.conn.execute(
                "SELECT COUNT(*) FROM app_tokens WHERE role='admin' AND user_id IS NULL AND revoked=0"
            ).fetchone()
            logger.warning(
                "APNs admin: 没有可推送设备！（%s活跃 admin token）",
                "有" if (admin_tokens and admin_tokens[0] > 0) else "无",
            )
    except Exception:
        logger.exception("APNs 启动诊断：get_active_devices_for_admin 失败")

    # 逐用户查关联
    for u in users:
        if not u.enabled:
            continue
        try:
            devs = st.get_active_devices_for_user(u.id)
        except Exception:
            logger.exception("APNs 启动诊断：get_active_devices_for_user 失败 user=%s", u.name)
            continue
        if devs:
            logger.info(
                "APNs 用户 %s (id=%s): %d 个可推送设备",
                u.name, u.id, len(devs),
            )
        else:
            # 查一下这个 user_id 有没有 token
            token_count = st.conn.execute(
                "SELECT COUNT(*) FROM app_tokens WHERE user_id = ? AND revoked = 0",
                (u.id,),
            ).fetchone()
            has_tokens = token_count and token_count[0] > 0
            logger.warning(
                "APNs 用户 %s (id=%s): 没有可推送设备！（该 user %s活跃 token）"
                "—— 请确认 iOS App 已用此账号登录并在 Settings 中注册了设备",
                u.name, u.id,
                "有" if has_tokens else "无",
            )
