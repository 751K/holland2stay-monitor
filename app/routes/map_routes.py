"""
路由：地图视图 + 地理编码

挂载的 endpoint
- GET  /map                      → map_view
- GET  /api/map                  → api_map（纯只读，仅返回已缓存坐标）
- GET  /api/map/locate           → api_map_locate（按 id 定位，绕过窗口与筛选）
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

from flask import Flask, abort, jsonify, render_template, request

from app.auth import admin_api_required, api_login_required, browse_required
from app.csrf import csrf_required
from app.db import storage
from app.services.listing_service import get_map_payload, locate_map_listing
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


@browse_required
def map_view() -> str:
    from app.i18n import get_lang
    from app.services.inbox_service import workspace_url
    from app.services.map_workspace import map_context
    from app.services.web_identity import current_listing_user

    zh=get_lang()=='zh'
    try:
        context=map_context(current_listing_user(),request.args,zh=zh)
    except ValueError as error:
        abort(400,description=str(error))
    def map_url(endpoint=None,**changes):
        defaults=dict(history='1' if context['map_history'] else None,
            positions=context['map_positions'],location=context['map_location'] or None,focus=None)
        if 'view' in changes or 'q' in changes:
            defaults['location']=None
        defaults.update(changes)
        return workspace_url(endpoint=endpoint,**defaults)
    context.update(inbox_url=map_url,zh=zh,google_maps_key=os.environ.get("GOOGLE_MAPS_API_KEY", ""))
    if request.accept_mimetypes.best=='application/json':
        response=jsonify(html=render_template('_map_side.html',**context),items=context['items'],
            markers=context['map_markers'],notice=context['map_notice'],
            selection=dict(history='1' if context['map_history'] else None,positions=context['map_positions'],
                location=context['map_location'] or None,page=context['page'],selected=context['selected']['id'] if context['selected'] else None),
            counts=dict(located=context['map_located_count'],unlocated=context['map_unlocated_count'],older=context['map_older_count']),
            unread_count=context['unread_count'])
        response.headers['Cache-Control']='no-store'
        return response
    return render_template('map.html',**context)


def _current_map_user():
    from app.services.web_identity import current_listing_user

    return current_listing_user()


@api_login_required
def api_map():
    """
    返回所有已缓存坐标的房源。

    纯只读——不触发外部 Photon 请求，不写数据库。
    未缓存地址的房源不包含 lat/lng，前端不渲染标记。
    需 geocode 时由 admin 通过 POST /api/map/geocode 手动启动。
    """
    return jsonify(get_map_payload(_current_map_user()))


@api_login_required
def api_map_locate():
    """按 id 定位一套房源，绕过新鲜度窗口与用户筛选。

    供 ``/map?focus=<id>`` 在「这个 id 不在已渲染集合里」时兜底。
    """
    listing_id = (request.args.get("id") or "").strip()
    if not listing_id:
        return jsonify({"ok": False, "reason": "not_found"}), 400
    return jsonify(locate_map_listing(listing_id))


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
        from app.runtime_config import web_runtime_environment
        from mstorage._map_calendar import _map_max_age_days
        listings = st.get_map_listings(max_age_days=_map_max_age_days(environ=web_runtime_environment()))
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


def api_buildings():
    """返回指定城市的所有楼盘（供用户过滤表单动态加载）。

    与 ``api_neighborhoods`` 同一形状：按城市收窄，因为库里楼盘有近百个
    （2026-09-10：H2S 58 个、Plaza 24 个），不收窄的下拉没法用。
    """
    cities = request.args.get("cities", "").split(",")
    cities = [c.strip() for c in cities if c.strip()]
    st = storage()
    try:
        buildings = st.get_feature_values("Building", cities=cities or None)
    except Exception:
        buildings = []
    finally:
        st.close()
    return jsonify({"buildings": buildings})


def register(app: Flask) -> None:
    app.add_url_rule("/map",                    endpoint="map_view",               view_func=map_view,               methods=["GET"])
    app.add_url_rule("/api/map",                endpoint="api_map",                view_func=api_map,                methods=["GET"])
    app.add_url_rule("/api/map/locate",         endpoint="api_map_locate",         view_func=api_map_locate,         methods=["GET"])
    app.add_url_rule("/api/map/geocode",        endpoint="api_map_geocode",        view_func=api_map_geocode,        methods=["POST"])
    app.add_url_rule("/api/map/geocode/status", endpoint="api_map_geocode_status", view_func=api_map_geocode_status, methods=["GET"])
    app.add_url_rule("/api/neighborhoods",      endpoint="api_neighborhoods",      view_func=api_neighborhoods,      methods=["GET"])
    app.add_url_rule("/api/buildings",          endpoint="api_buildings",          view_func=api_buildings,          methods=["GET"])
