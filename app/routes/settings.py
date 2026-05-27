"""
路由：全局配置（/settings）

挂载的 endpoint
- GET/POST /settings → settings
"""
from __future__ import annotations

import logging
from typing import Any

from dotenv import dotenv_values
from flask import Flask, flash, redirect, render_template, request, url_for

from config import ENV_PATH, KNOWN_CITIES, KNOWN_OURDOMAIN_CITIES, KNOWN_XIOR_CITIES

from app.auth import admin_required
from app.csrf import csrf_required
from app.env_writer import write_env_key
from app.i18n import get_lang
from app.safety import sanitize_dotenv
from translations import tr

logger = logging.getLogger(__name__)

# 全局配置可写入的 .env 键（通知/过滤/预订已移至 SQLite user_configs）
SETTINGS_KEYS: list[str] = [
    "CHECK_INTERVAL", "LOG_LEVEL",
    # 智能轮询
    "PEAK_INTERVAL", "MIN_INTERVAL", "PEAK_START", "PEAK_END", "PEAK_START_2", "PEAK_END_2", "PEAK_WEEKDAYS_ONLY", "JITTER_RATIO",
    # 心跳
    "HEARTBEAT_INTERVAL_MINUTES",
]

# 数值型 key：空值或非法值跳过写入，避免 load_config() 中 int("") / int("abc") 抛错
_NUMERIC_KEYS = frozenset({
    "CHECK_INTERVAL", "PEAK_INTERVAL", "MIN_INTERVAL", "JITTER_RATIO",
    "HEARTBEAT_INTERVAL_MINUTES",
})
_FLOAT_KEYS = frozenset({"JITTER_RATIO"})


@admin_required
@csrf_required
def settings() -> Any:
    lang = get_lang()
    if request.method == "POST":
        if not ENV_PATH.exists():
            ENV_PATH.touch()

        selected_sources = request.form.getlist("source_selected")
        allowed_sources = {"holland2stay", "ourdomain", "xior"}
        sources = [s for s in selected_sources if s in allowed_sources]
        if not sources:
            sources = ["holland2stay"]
            flash(tr("settings_no_source", lang), "warning")
        sources_val = ",".join(sources)
        write_env_key("SOURCES", sanitize_dotenv(sources_val))

        # 城市：复选框提交 "CityName,ID" 格式，用 | 拼接
        selected_cities = request.form.getlist("city_selected")
        cities_val = "|".join(selected_cities) if selected_cities else "Eindhoven,29"
        write_env_key("CITIES", sanitize_dotenv(cities_val))

        selected_od_cities = request.form.getlist("ourdomain_city_selected")
        od_cities_val = "|".join(selected_od_cities) if selected_od_cities else "Amsterdam Diemen,diemen"
        write_env_key("OURDOMAIN_CITIES", sanitize_dotenv(od_cities_val))

        selected_xr_cities = request.form.getlist("xior_city_selected")
        xr_cities_val = "|".join(selected_xr_cities) if selected_xr_cities else ""
        write_env_key("XIOR_CITIES", sanitize_dotenv(xr_cities_val))

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
            write_env_key(key, sanitized)

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
            cities_val,
            od_cities_val,
        )

        flash(tr("settings_config_saved", lang), "success")
        return redirect(url_for("settings"))

    env = dict(dotenv_values(str(ENV_PATH)))

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
        known_cities=KNOWN_CITIES,
        known_ourdomain_cities=KNOWN_OURDOMAIN_CITIES,
        known_xior_cities=KNOWN_XIOR_CITIES,
        xior_by_city=xior_by_city,
        xior_city_all_checked=xior_city_all_checked,
        selected_sources=selected_sources,
        selected_city_ids=selected_city_ids,
        selected_ourdomain_keys=selected_ourdomain_keys,
        selected_xior_keys=selected_xior_keys,
    )


def register(app: Flask) -> None:
    app.add_url_rule("/settings", endpoint="settings", view_func=settings, methods=["GET", "POST"])
