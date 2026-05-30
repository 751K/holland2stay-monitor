"""
API v1 蓝图工厂
================

挂载点 ``/api/v1/*``，专供 iOS / 第三方客户端使用：

- 所有响应统一壳形：``{ok, data}`` / ``{ok, error: {code, message}}``
- 鉴权走 Bearer Token（``Authorization: Bearer xxx``），与 Web 后台
  cookie session 完全隔离
- CSRF 不适用（Bearer 不来自浏览器 cookie），各路由用 ``@bearer_required``
  自行守门即可

子模块
------
- auth          : /auth/login, /auth/logout, /auth/me
- stats_public  : /stats/public/*   ← guest 也可访问

后续 Phase 增量：listings / map / calendar / notifications / devices / me / admin。
"""

from __future__ import annotations

import hashlib

from flask import Blueprint, Flask, request

from . import admin as _admin
from . import auth as _auth
from . import calendar as _calendar
from . import devices as _devices
from . import diagnostics as _diagnostics
from . import feedback as _feedback
from . import legal as _legal
from . import listings as _listings
from . import map as _map
from . import me as _me
from . import notifications as _notifications
from . import stats_public as _stats_public


# GET 读端点的客户端缓存窗口（秒）。10s 足够覆盖"快速切 tab"场景：
# 窗口内 URLSession 直接用本地缓存零网络；超窗后带 If-None-Match 复验，
# 服务端返 304（无 body）或 200（有变更）。对房源这种分钟级变化的数据，
# 10s 陈旧度完全安全，且 SSE / 下拉刷新仍提供实时更新。
_API_CACHE_MAX_AGE = 10


def _apply_conditional_cache(resp):
    """
    给 ``GET /api/v1/*`` 的 200 JSON 响应加 ETag + Cache-Control，并处理
    ``If-None-Match`` 条件请求（命中则降级为 304，省去 body 传输）。

    只对 GET + 200 + JSON 生效——POST/PATCH（登录、标记已读、下单等）和
    错误响应都不缓存。流式响应（SSE）没有 direct_passthrough=False，跳过。

    freshness 保障
    --------------
    ``Cache-Control: private, max-age=10, must-revalidate``：
    - private  : 只许客户端缓存，不许中间代理/CDN 缓存（含用户私有数据）
    - max-age=10 : 10s 内视为新鲜，URLSession 直接用缓存
    - must-revalidate : 过期后必须回源复验，绝不使用过期副本
    ETag 是 body 的 md5，内容不变则复验返回 304。
    """
    if request.method != "GET" or resp.status_code != 200:
        return resp
    if resp.direct_passthrough:
        return resp  # 流式响应（SSE）不缓存
    ctype = resp.headers.get("Content-Type", "")
    if "application/json" not in ctype:
        return resp

    body = resp.get_data()
    etag = '"' + hashlib.md5(body).hexdigest() + '"'  # noqa: S324  (缓存校验用，非安全用途)
    resp.headers["ETag"] = etag

    # 通知列表是高频客户端变更端点（markRead 频繁改读状态）。若用 max-age，
    # markRead(POST) 之后 10s 内的 GET 可能命中陈旧缓存（仍显示未读）→ UI 回退。
    # 改用 no-cache（每次复验）：仍享受 304 省 body 下载，但绝不发陈旧列表。
    # 其它端点（listings / map / calendar / stats / me.summary）客户端不直接
    # 改写，max-age=10 安全——快速切 tab 零网络。
    if "/notifications" in request.path:
        resp.headers["Cache-Control"] = "private, no-cache"
    else:
        resp.headers["Cache-Control"] = f"private, max-age={_API_CACHE_MAX_AGE}, must-revalidate"

    # 条件请求：客户端带的 If-None-Match 命中当前 ETag → 304（不回 body）
    inm = request.headers.get("If-None-Match", "")
    if inm and etag in [t.strip() for t in inm.split(",")]:
        resp.set_data(b"")
        resp.status_code = 304
    return resp


def register(app: Flask) -> None:
    """主入口：在 web.py 里调一次。"""
    bp = Blueprint("api_v1", __name__, url_prefix="/api/v1")
    bp.after_request(_apply_conditional_cache)
    _auth.register(bp)
    _stats_public.register(bp)
    _listings.register(bp)
    _map.register(bp)
    _calendar.register(bp)
    _notifications.register(bp)
    _me.register(bp)
    _devices.register(bp)
    _feedback.register(bp)
    _legal.register(bp)
    _diagnostics.register(bp)
    _admin.register(bp)
    app.register_blueprint(bp)
