"""
路由：全局配置（/settings）

挂载的 endpoint
- GET/POST /settings → settings
"""
from __future__ import annotations

import logging
from typing import Any

from flask import Flask, flash, redirect, render_template, request, url_for

from config import (KNOWN_SOURCES, source_display_name, KNOWN_CITIES,
                    KNOWN_MAGIS_CITIES, KNOWN_OURDOMAIN_CITIES,
                    KNOWN_PLAZA_CITIES,
                    KNOWN_STUDENTEXPERIENCE_CITIES, KNOWN_XIOR_CITIES)

from app.auth import admin_required
from app.csrf import csrf_required
from app.db import storage
from app.i18n import get_lang
from app.process_ctrl import write_reload_request
from app.safety import sanitize_dotenv
from env_registry import RUNTIME_KEYS
from settings_store import source_of
from target_config import validate as validate_structured
from translations import tr

logger = logging.getLogger(__name__)

#: 写入 app_settings 时的来源标记。与 "migration" 区分开，便于事后追查一个值
#: 到底是迁移带过来的还是有人手点的。
_UPDATED_BY = "panel"

# 全局配置可写入的键（通知/过滤/预订已移至 SQLite user_configs）。
#
# 这些值住在 app_settings 表，不再写 .env——.env 同时被人和程序写，是
# app/env_writer.py 那把锁与 write_env_key() 不能用 os.replace() 的根源。
# 取值顺序见 settings_store：真实环境变量 > app_settings > 代码默认值。
SETTINGS_KEYS: list[str] = [
    "CHECK_INTERVAL", "LOG_LEVEL",
    # 智能轮询
    "PEAK_INTERVAL", "MIN_INTERVAL", "PEAK_START", "PEAK_END", "PEAK_START_2", "PEAK_END_2", "PEAK_WEEKDAYS_ONLY", "JITTER_RATIO",
    # 心跳
    "HEARTBEAT_INTERVAL_MINUTES",
    # 地图显示范围
    "MAP_MAX_AGE_DAYS",
]

# 数值型 key：空值或非法值跳过写入，避免 load_config() 中 int("") / int("abc") 抛错
_NUMERIC_KEYS = frozenset({
    "CHECK_INTERVAL", "PEAK_INTERVAL", "MIN_INTERVAL", "JITTER_RATIO",
    "HEARTBEAT_INTERVAL_MINUTES", "MAP_MAX_AGE_DAYS",
})
_FLOAT_KEYS = frozenset({"JITTER_RATIO"})


@admin_required
@csrf_required
def settings() -> Any:
    lang = get_lang()
    st = storage()
    if request.method == "POST":
        # 整批攒齐再一次事务写入。逐条提交的话中途失败会留下一半新一半旧，
        # 例如 MIN_INTERVAL 已改而 PEAK_INTERVAL 还是旧的，组合起来可能非法。
        pending: dict[str, str] = {}

        selected_sources = request.form.getlist("source_selected")
        # 用 config.KNOWN_SOURCES（模块级已导入；**别在这里再 import 一次**，
        # 函数内的 import 会把它变成整个函数的局部变量，GET 分支渲染时就会
        # UnboundLocalError）。原先这里写死了三个平台，漏掉 ourcampus，从面板
        # 保存一次就会把它从 SOURCES 里悄悄删掉。
        sources = [s for s in selected_sources if s in KNOWN_SOURCES]
        if not sources:
            sources = ["holland2stay"]
            flash(tr("settings_no_source", lang), "warning")
        sources_val = ",".join(sources)
        pending["SOURCES"] = sanitize_dotenv(sources_val)

        # 城市：复选框提交 "CityName,ID" 格式，用 | 拼接。
        #
        # **全部取消勾选就是空**，不回落到硬编码的默认城市。原先 CITIES 和
        # OURDOMAIN_CITIES 在空选时会写回 "Eindhoven,29" / "Amsterdam Diemen,
        # diemen"，于是用户取消勾选、保存，刷新一看又勾上了——界面把没保存的
        # 东西显示成保存了，这比拒绝保存还糟。XIOR_CITIES 一直是允许空的，
        # 三者现在一致。
        #
        # 空列表是合法配置：monitor 那边「未配置任何抓取任务」有专门的分支，
        # 只跳过本轮并 WARNING，不会出错。真要整个停掉某平台，取消勾选平台
        # 本身更直接——所以下面只提示，不代劳。
        city_lists = {
            "CITIES": request.form.getlist("city_selected"),
            "OURDOMAIN_CITIES": request.form.getlist("ourdomain_city_selected"),
            "XIOR_CITIES": request.form.getlist("xior_city_selected"),
            "MAGIS_CITIES": request.form.getlist("magis_city_selected"),
            "STUDENTEXPERIENCE_CITIES":
                request.form.getlist("se_city_selected"),
            "PLAZA_CITIES": request.form.getlist("plaza_city_selected"),
        }
        for key, picked in city_lists.items():
            pending[key] = sanitize_dotenv("|".join(picked))

        # 平台开着却一个楼盘都没勾——配置合法，但十有八九不是本意，说一声。
        # 这里**只警告不修改**：和 SOURCES 那条「至少留一个平台」不同，那个
        # 若为空整个监控就是空转，这个只是某一个平台没目标。
        #
        # ⚠️ 三个列表的「空」含义**并不相同**，别把它们一视同仁：
        #
        #     CITIES            空 → 0 个城市，该平台不抓
        #     OURDOMAIN_CITIES  空 → 0 个楼盘，该平台不抓
        #     XIOR_CITIES       空 → **全部 30 栋**（config.py 的既有约定）
        #     MAGIS_CITIES      空 → **全部 5 城**（同上）
        #     STUDENTEXPERIENCE_CITIES
        #                       空 → **全部 2 城**（同上）
        #     PLAZA_CITIES      空 → **全部 12 城**（同上）
        #
        # 所以 xior / magis / studentexperience / plaza 不在下面这张表里：它们
        # 空着不是「没目标」，恰恰是「全都要」，对它们报「不会抓取」是错的。
        _no_target_when_empty = {
            "CITIES": "holland2stay",
            "OURDOMAIN_CITIES": "ourdomain",
        }
        empty_enabled = sorted(
            source_display_name(src)
            for key, src in _no_target_when_empty.items()
            if not city_lists[key] and src in sources
        )
        if empty_enabled:
            flash(
                tr("settings_source_no_target", lang).format(
                    sources="、".join(empty_enabled) if lang == "zh"
                    else ", ".join(empty_enabled)),
                "warning",
            )

        new_values: dict[str, str] = {}
        for key in SETTINGS_KEYS:
            val = request.form.get(key, "")
            sanitized = sanitize_dotenv(val)
            # 数值型 key：空值或非法数字不写入，保留 .env 旧值
            if key in _NUMERIC_KEYS:
                if sanitized == "":
                    new_values[key] = "(未改)"
                    continue
                try:
                    float(sanitized) if key in _FLOAT_KEYS else int(sanitized)
                except ValueError:
                    new_values[key] = f"(非法值: {sanitized!r})"
                    continue
            new_values[key] = sanitized
            pending[key] = sanitized

        logger.info(
            "全局配置已保存 — sources=%s 间隔=%s 高峰=%s–%s(%s–%s/%s–%s) 仅工作日=%s 抖动=%s 心跳=%smin 日志=%s H2S城市=%s OD楼盘=%s",
            sources_val,
            new_values.get("CHECK_INTERVAL", "?"),
            new_values.get("MIN_INTERVAL", "?"), new_values.get("PEAK_INTERVAL", "?"),
            new_values.get("PEAK_START", "?"), new_values.get("PEAK_END", "?"),
            new_values.get("PEAK_START_2", "?"), new_values.get("PEAK_END_2", "?"),
            new_values.get("PEAK_WEEKDAYS_ONLY", "?"),
            new_values.get("JITTER_RATIO", "?"),
            new_values.get("HEARTBEAT_INTERVAL_MINUTES", "?"),
            new_values.get("LOG_LEVEL", "?"),
            pending["CITIES"],
            pending["OURDOMAIN_CITIES"],
        )

        # 写之前先校验结构化的那几项。它们是分隔符拼出来的字符串，坏掉的后果
        # 不一致：有的静默丢弃（CITIES 解析失败 → 0 个城市，监控照跑但什么都不
        # 抓），有的直接让 load_config() 抛 ValueError，monitor 起不来。
        # 挡在写入之前，坏值就进不了库。
        problems = validate_structured(pending)
        fatal = [p for p in problems if p.fatal]
        if fatal:
            for p in fatal:
                logger.warning("配置校验失败，已拒绝保存：%s", p)
            flash(
                tr("settings_invalid_value", lang) + " " + "；".join(str(p) for p in fatal),
                "error",
            )
            return redirect(url_for("settings"))
        for p in problems:
            # 非致命的（ID 不在已知表里）照常保存——官方注册表会更新，写死拒绝
            # 会让一个新上线的城市变成保存失败。但要说出来。
            logger.info("配置校验提示：%s", p)
            flash(str(p), "warning")

        st.set_app_settings(pending, updated_by=_UPDATED_BY)
        # 让 monitor 立刻重读，而不是等到下次重启。改完配置要等一个不确定的时长
        # 才生效，人会以为没保存上，于是再点一次。
        try:
            write_reload_request()
        except OSError:
            logger.warning("写热重载请求失败，配置将在 monitor 下次重启后生效", exc_info=True)

        overridden = sorted(k for k in pending if source_of(k) == "env")
        if overridden:
            # 真实环境变量压着数据库，面板显示的和进程实际用的不是一回事。
            # 不说出来就是一次静默失败。
            flash(
                "以下配置存在环境变量覆盖，本次修改不会生效："
                + "、".join(overridden),
                "warning",
            )

        flash(tr("settings_config_saved", lang), "success")
        return redirect(url_for("settings"))

    # 从数据库读，不从 os.environ 读：gunicorn 多个 worker 各有自己的 os.environ，
    # 只有写入的那个 worker 注水过，其余的会显示旧值。数据库是唯一都看得到的。
    # 模板对每个键都自带默认值（env.get(key, '300') 之类），缺键即回落到默认。
    env = {k: v for k, v in st.all_app_settings().items() if k in RUNTIME_KEYS}
    env_overridden = sorted(k for k in RUNTIME_KEYS if source_of(k) == "env")
    setting_meta = st.app_settings_meta()

    selected_sources = {
        s.strip().lower()
        for s in (env.get("SOURCES") or "holland2stay").replace("|", ",").split(",")
        if s.strip()
    }
    if not selected_sources:
        selected_sources = {"holland2stay"}

    selected_city_ids: set[str] = set()
    for entry in env.get("CITIES", "Eindhoven,29").split("|"):
        parts = entry.strip().split(",")
        if len(parts) >= 2:
            selected_city_ids.add(parts[-1].strip())

    selected_ourdomain_keys: set[str] = set()
    for entry in env.get("OURDOMAIN_CITIES", "Amsterdam Diemen,diemen").split("|"):
        parts = entry.strip().split(",")
        if len(parts) >= 2:
            selected_ourdomain_keys.add(parts[-1].strip())

    # 空 = 全部（与 XIOR_CITIES 同一约定），所以未配置时全部勾上，
    # 而不是一个都不勾——后者会让用户以为默认什么都不抓。
    selected_magis_keys: set[str] = set()
    raw_magis = env.get("MAGIS_CITIES", "")
    if raw_magis:
        for entry in raw_magis.split("|"):
            parts = entry.strip().split(",")
            if len(parts) >= 2:
                selected_magis_keys.add(parts[-1].strip())
    else:
        selected_magis_keys = {c["key"] for c in KNOWN_MAGIS_CITIES}

    selected_se_keys: set[str] = set()
    raw_se = env.get("STUDENTEXPERIENCE_CITIES", "")
    if raw_se:
        for entry in raw_se.split("|"):
            parts = entry.strip().split(",")
            if len(parts) >= 2:
                selected_se_keys.add(parts[-1].strip())
    else:
        selected_se_keys = {c["key"] for c in KNOWN_STUDENTEXPERIENCE_CITIES}

    selected_plaza_keys: set[str] = set()
    raw_plaza = env.get("PLAZA_CITIES", "")
    if raw_plaza:
        for entry in raw_plaza.split("|"):
            parts = entry.strip().split(",")
            if len(parts) >= 2:
                selected_plaza_keys.add(parts[-1].strip())
    else:
        selected_plaza_keys = {c["key"] for c in KNOWN_PLAZA_CITIES}

    selected_xior_keys: set[str] = set()
    raw_xior = env.get("XIOR_CITIES", "")
    if raw_xior:
        for entry in raw_xior.split("|"):
            parts = entry.strip().split(",")
            if len(parts) >= 2:
                selected_xior_keys.add(parts[-1].strip())
    else:
        selected_xior_keys = {c["key"] for c in KNOWN_XIOR_CITIES}

    xior_by_city: dict[str, list[dict]] = {}
    xior_city_all_checked: dict[str, bool] = {}
    for c in KNOWN_XIOR_CITIES:
        xior_by_city.setdefault(c["city"], []).append(c)
    for city, buildings in xior_by_city.items():
        xior_city_all_checked[city] = any(b["key"] in selected_xior_keys for b in buildings)

    return render_template(
        "settings.html",
        env=env,
        env_overridden=env_overridden,
        setting_meta=setting_meta,
        known_cities=KNOWN_CITIES,
        known_ourdomain_cities=KNOWN_OURDOMAIN_CITIES,
        known_magis_cities=KNOWN_MAGIS_CITIES,
        known_se_cities=KNOWN_STUDENTEXPERIENCE_CITIES,
        known_plaza_cities=KNOWN_PLAZA_CITIES,
        known_xior_cities=KNOWN_XIOR_CITIES,
        xior_by_city=xior_by_city,
        xior_city_all_checked=xior_city_all_checked,
        selected_sources=selected_sources,
        source_options=[(k, source_display_name(k)) for k in KNOWN_SOURCES],
        selected_city_ids=selected_city_ids,
        selected_ourdomain_keys=selected_ourdomain_keys,
        selected_magis_keys=selected_magis_keys,
        selected_se_keys=selected_se_keys,
        selected_plaza_keys=selected_plaza_keys,
        selected_xior_keys=selected_xior_keys,
    )


def register(app: Flask) -> None:
    app.add_url_rule("/settings", endpoint="settings", view_func=settings, methods=["GET", "POST"])
