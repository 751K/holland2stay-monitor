"""
判断当前是不是官方部署（flatradar.app）。

别人用这份代码自己部署时，页面要在品牌名后面标「自部署版本」，并弹一次免责提示——
访客得知道这个站不是作者在运营，出了问题找部署者。

判定只看域名，两处任一命中即算官方：
- 请求的 Host —— 访客实际打开的地址；
- PUBLIC_BASE_URL —— 反代改写了 Host 时的兜底（邮箱验证链接也用它，自部署的人
  不可能把它填成 flatradar.app，否则验证链接会指到别人的站）。

本地开发（localhost）不算官方，正好能直接看到自部署时的样子。
"""
from __future__ import annotations

import os
from urllib.parse import urlparse

from flask import request

OFFICIAL_HOSTS = frozenset({"flatradar.app", "www.flatradar.app"})


def is_official_site() -> bool:
    host = (request.host or "").rsplit(":", 1)[0].strip("[]").lower()
    if host in OFFICIAL_HOSTS:
        return True
    base = urlparse(os.environ.get("PUBLIC_BASE_URL", "").strip()).hostname or ""
    return base.lower() in OFFICIAL_HOSTS
