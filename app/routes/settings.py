"""
路由：全局配置（/settings）

挂载的 endpoint
- GET/POST /settings → settings
"""
from __future__ import annotations

from typing import Any

from dotenv import dotenv_values
from flask import Flask, flash, redirect, render_template, request, url_for

from config import ENV_PATH, KNOWN_CITIES

from app.auth import admin_required
from app.csrf import csrf_required
from app.env_writer import write_env_key
from app.safety import sanitize_dotenv

# 全局配置可写入的 .env 键（通知/过滤/预订已移至 users.json）
SETTINGS_KEYS: list[str] = [
    "CHECK_INTERVAL", "LOG_LEVEL",
    # 智能轮询
    "PEAK_INTERVAL", "MIN_INTERVAL", "PEAK_START", "PEAK_END", "PEAK_WEEKDAYS_ONLY", "JITTER_RATIO",
]


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

        for key in SETTINGS_KEYS:
            val = request.form.get(key, "")
            write_env_key(key, sanitize_dotenv(val))

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
