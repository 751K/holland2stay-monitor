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

from flask import Blueprint, Flask

from . import admin as _admin
from . import auth as _auth
from . import calendar as _calendar
from . import devices as _devices
from . import listings as _listings
from . import map as _map
from . import me as _me
from . import notifications as _notifications
from . import stats_public as _stats_public


def register(app: Flask) -> None:
    """主入口：在 web.py 里调一次。"""
    bp = Blueprint("api_v1", __name__, url_prefix="/api/v1")
    _auth.register(bp)
    _stats_public.register(bp)
    _listings.register(bp)
    _map.register(bp)
    _calendar.register(bp)
    _notifications.register(bp)
    _me.register(bp)
    _devices.register(bp)
    _admin.register(bp)
    app.register_blueprint(bp)
