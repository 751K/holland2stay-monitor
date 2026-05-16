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

from config import ENV_PATH, KNOWN_CITIES

from app.auth import admin_required
from app.csrf import csrf_required
from app.env_writer import write_env_key
from app.safety import sanitize_dotenv

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
    if request.method == "POST":
        if not ENV_PATH.exists():
            ENV_PATH.touch()

        # 城市：复选框提交 "CityName,ID" 格式，用 | 拼接
        selected_cities = request.form.getlist("city_selected")
        cities_val = "|".join(selected_cities) if selected_cities else "Eindhoven,29"
        write_env_key("CITIES", sanitize_dotenv(cities_val))

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
            "全局配置已保存 — 间隔=%s 高峰=%s–%s(%s–%s/%s–%s) 仅工作日=%s 抖动=%s 心跳=%smin 日志=%s 城市=%s",
            new_values.get("CHECK_INTERVAL", "?"),
            new_values.get("MIN_INTERVAL", "?"), new_values.get("PEAK_INTERVAL", "?"),
            new_values.get("PEAK_START", "?"), new_values.get("PEAK_END", "?"),
            new_values.get("PEAK_START_2", "?"), new_values.get("PEAK_END_2", "?"),
            new_values.get("PEAK_WEEKDAYS_ONLY", "?"),
            new_values.get("JITTER_RATIO", "?"),
            new_values.get("HEARTBEAT_INTERVAL_MINUTES", "?"),
            new_values.get("LOG_LEVEL", "?"),
            cities_val,
        )

        flash("✅ 全局配置已保存", "success")
        return redirect(url_for("settings"))

    env = dict(dotenv_values(str(ENV_PATH)))

    selected_city_ids: set[str] = set()
    for entry in env.get("CITIES", "Eindhoven,29").split("|"):
        parts = entry.strip().split(",")
        if len(parts) >= 2:
            selected_city_ids.add(parts[-1].strip())

    return render_template(
        "settings.html",
        env=env,
        known_cities=KNOWN_CITIES,
        selected_city_ids=selected_city_ids,
    )


def register(app: Flask) -> None:
    app.add_url_rule("/settings", endpoint="settings", view_func=settings, methods=["GET", "POST"])
