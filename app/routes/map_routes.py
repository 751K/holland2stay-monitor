"""
路由：地图视图 + 地理编码

挂载的 endpoint
- GET  /map                      → map_view
- GET  /api/map                  → api_map（纯只读，仅返回已缓存坐标）
- POST /api/map/geocode          → api_map_geocode（手动启动）
- GET  /api/map/geocode/status   → api_map_geocode_status
- GET  /api/neighborhoods        → api_neighborhoods

模块级状态（_geocode_lock + _geocode_status）必须留在此处，
确保同一进程内的并发请求看到同一份任务状态。
"""
from __future__ import annotations

import logging
import os
import threading

from flask import Flask, jsonify, render_template, request

from app.auth import admin_api_required, api_login_required, login_required
from app.csrf import csrf_required
from app.db import storage
from app.services.listing_service import get_map_payload
from mcore.geocode import geocode_addresses

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
# 后台地理编码任务的共享状态
# ------------------------------------------------------------------ #
_geocode_lock = threading.Lock()
_geocode_status: dict = {"running": False, "total": 0, "done": 0, "failed": 0, "errors": []}


def _run_geocode_worker(addresses: list[str]) -> None:
    """后台线程：逐个解析地址，进度更新到模块级状态。

    解析本身在 mcore.geocode——监控进程的周期任务用的是同一份实现。这里只负责
    把进度暴露给 /api/map/geocode/status。
    """
    st = storage()
    try:
        def _progress(done: int, failed: int, errors: list[dict]) -> None:
            with _geocode_lock:
                _geocode_status["done"] = done
                _geocode_status["failed"] = failed

        _, _, errors = geocode_addresses(st, addresses, on_progress=_progress)
    finally:
        st.close()
        with _geocode_lock:
            _geocode_status["running"] = False
            _geocode_status["errors"] = errors[:20]   # 最多保留 20 条


@login_required
def map_view() -> str:
    return render_template(
        "map.html",
        google_maps_key=os.environ.get("GOOGLE_MAPS_API_KEY", ""),
    )


@api_login_required
def api_map():
    """
    返回所有已缓存坐标的房源。

    纯只读——不触发外部 Photon 请求，不写数据库。
    未缓存地址的房源不包含 lat/lng，前端不渲染标记。
    需 geocode 时由 admin 通过 POST /api/map/geocode 手动启动。
    """
    return jsonify(get_map_payload())


@admin_api_required
@csrf_required
def api_map_geocode():
    """启动后台地理编码任务。进度通过 GET /api/map/geocode/status 查询。"""
    with _geocode_lock:
        if _geocode_status["running"]:
            s = dict(_geocode_status)
            return jsonify({"ok": True, "running": True, "total": s["total"], "done": s["done"], "failed": s["failed"]})

    st = storage()
    try:
        listings = st.get_map_listings()
        uncached = [l for l in listings if not st.get_cached_coords(l["address"])]
    finally:
        st.close()

    if not uncached:
        with _geocode_lock:
            _geocode_status["errors"] = []
        return jsonify({"ok": True, "total": 0, "done": 0, "failed": 0, "running": False, "finished": True})

    with _geocode_lock:
        _geocode_status["running"] = True
        _geocode_status["total"] = len(uncached)
        _geocode_status["done"] = 0
        _geocode_status["failed"] = 0
        _geocode_status["errors"] = []

    addrs = [l["address"] for l in uncached]
    threading.Thread(target=_run_geocode_worker, args=(addrs,), daemon=True).start()
    return jsonify({"ok": True, "running": True, "total": len(uncached), "done": 0, "failed": 0})


@api_login_required
def api_map_geocode_status():
    """查询地理编码任务进度。"""
    with _geocode_lock:
        s = dict(_geocode_status)
    return jsonify({
        "running": s["running"], "total": s["total"], "done": s["done"], "failed": s["failed"],
        "finished": not s["running"] and s["total"] > 0,
        "errors": s.get("errors", []),
    })


@api_login_required
def api_neighborhoods():
    """返回指定城市的所有片区（供用户过滤表单动态加载）。"""
    cities = request.args.get("cities", "").split(",")
    cities = [c.strip() for c in cities if c.strip()]
    st = storage()
    try:
        hoods = st.get_feature_values("Neighborhood", cities=cities or None)
    except Exception:
        hoods = []
    finally:
        st.close()
    return jsonify({"neighborhoods": hoods})


def register(app: Flask) -> None:
    app.add_url_rule("/map",                    endpoint="map_view",               view_func=map_view,               methods=["GET"])
    app.add_url_rule("/api/map",                endpoint="api_map",                view_func=api_map,                methods=["GET"])
    app.add_url_rule("/api/map/geocode",        endpoint="api_map_geocode",        view_func=api_map_geocode,        methods=["POST"])
    app.add_url_rule("/api/map/geocode/status", endpoint="api_map_geocode_status", view_func=api_map_geocode_status, methods=["GET"])
    app.add_url_rule("/api/neighborhoods",      endpoint="api_neighborhoods",      view_func=api_neighborhoods,      methods=["GET"])
