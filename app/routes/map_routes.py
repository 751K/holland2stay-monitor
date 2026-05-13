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

import json
import threading
import time as _time

from flask import Flask, jsonify, render_template, request

from app.auth import admin_api_required, api_login_required, login_required
from app.csrf import csrf_required
from app.db import storage

# ------------------------------------------------------------------ #
# 后台地理编码任务的共享状态
# ------------------------------------------------------------------ #
_geocode_lock = threading.Lock()
_geocode_status: dict = {"running": False, "total": 0, "done": 0, "failed": 0, "errors": []}


def _geocode_one(addr: str) -> tuple[float, float] | None:
    """对单个地址做 Photon 地理编码；含 Room 房号则失败时回退到建筑地址重试。"""
    from urllib.request import Request, urlopen
    from urllib.parse import quote

    def _query(q: str) -> tuple[float, float] | None:
        url = f"https://photon.komoot.io/api/?q={quote(q)}&limit=1"
        req = Request(url, headers={"User-Agent": "Holland2StayMonitor/1.0"})
        resp = urlopen(req, timeout=5)
        data = json.loads(resp.read().decode())
        feats = data.get("features", [])
        if feats:
            coords = feats[0]["geometry"]["coordinates"]
            return float(coords[1]), float(coords[0])  # (lat, lng)
        return None

    result = _query(addr)
    if result is not None:
        return result

    # 含 Room 编号的地址（如 "Westblaak 924 Room 2"）Photon 往往无结果；
    # 回退到建筑级地址（去掉 Room X）重试
    import re
    stripped = re.sub(r"\bRoom\s+\S+", "", addr, flags=re.IGNORECASE).strip().rstrip(",")
    if stripped != addr:
        try:
            return _query(stripped)
        except Exception:
            pass
    return None


def _run_geocode_worker(addresses: list[str]) -> None:
    """后台线程：逐个地理编码地址列表，结果写入缓存，进度更新到全局状态。"""
    from urllib.request import Request, urlopen
    from urllib.parse import quote

    st = storage()
    done, failed = 0, 0
    errors: list[dict] = []
    try:
        for addr in addresses:
            try:
                coord = _geocode_one(addr)
                if coord:
                    st.cache_coords(addr, coord[0], coord[1])
                    done += 1
                else:
                    failed += 1
                    errors.append({"address": addr, "reason": "Photon returned no results"})
            except Exception as e:
                failed += 1
                errors.append({"address": addr, "reason": str(e)[:120]})
            with _geocode_lock:
                _geocode_status["done"] = done
                _geocode_status["failed"] = failed
            _time.sleep(0.15)
    finally:
        st.close()
        with _geocode_lock:
            _geocode_status["running"] = False
            _geocode_status["errors"] = errors[:20]  # 最多保留 20 条


@login_required
def map_view() -> str:
    return render_template("map.html")


@api_login_required
def api_map():
    """
    返回所有已缓存坐标的房源。

    纯只读——不触发外部 Photon 请求，不写数据库。
    未缓存地址的房源不包含 lat/lng，前端不渲染标记。
    需 geocode 时由 admin 通过 POST /api/map/geocode 手动启动。
    """
    results: list[dict] = []
    uncached = 0
    st = storage()
    try:
        listings = st.get_map_listings()
        for l in listings:
            cached = st.get_cached_coords(l["address"])
            if cached:
                lat, lng = cached
                results.append({**l, "lat": lat, "lng": lng})
            else:
                uncached += 1
    finally:
        st.close()

    return jsonify({"listings": results, "uncached": uncached})


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
