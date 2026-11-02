"""出错页：页面请求出错时给一个和公开页同一套外壳的说明页，代替 Flask 自带的白底黑字。

只接管**页面**请求。``/api/`` 下的接口和明确要 JSON 的请求原样返回，
App 和前端脚本读的是状态码和 JSON，不能突然拿到一页 HTML。

未捕获的异常照常进日志（Flask 在调用 500 处理器之前已经记过 traceback），
页面上只给固定文案，不露异常内容。
"""
from __future__ import annotations

import logging

from flask import Flask, render_template, request
from werkzeug.exceptions import HTTPException, InternalServerError

from app.i18n import get_lang

logger = logging.getLogger(__name__)

# (标题, 一句说明)，中英各一份；没列出的状态码用通用文案
_COPY = {
    400: (("请求无效", "这个请求的格式不对。"),
          ("Bad request", "The request could not be understood.")),
    401: (("需要登录", "登录后才能查看这一页。"),
          ("Sign in required", "Sign in to see this page.")),
    403: (("没有权限", "你没有权限访问这一页；如果是提交表单时出现，刷新页面后再试。"),
          ("Not allowed", "You don't have access to this page. If you were submitting a form, reload and try again.")),
    404: (("找不到这一页", "链接可能写错了，或者这一页已经不在了。"),
          ("Page not found", "The link may be wrong, or the page no longer exists.")),
    405: (("不支持这个操作", "这一页不接受这种请求。"),
          ("Not supported", "This page doesn't accept that kind of request.")),
    429: (("请求太频繁", "稍等一会儿再试。"),
          ("Too many requests", "Wait a moment and try again.")),
    500: (("服务器出错了", "我们这边出了问题，稍后再试。"),
          ("Something went wrong", "Something broke on our side. Try again later.")),
}


def _wants_json() -> bool:
    if request.path.startswith("/api/"):
        return True
    best = request.accept_mimetypes.best_match(["text/html", "application/json"])
    return best == "application/json" and not request.accept_mimetypes.accept_html


def _page(code: int, fallback):
    zh = get_lang() == "zh"
    pair = _COPY.get(code) or _COPY[500 if code >= 500 else 400]
    title, message = pair[0] if zh else pair[1]
    try:
        return render_template("error.html", code=code, title=title, message=message), code
    except Exception:
        # 出错页自己渲染失败（比如模板上下文里的数据库读挂了）就退回原始响应，不要再抛一层
        logger.exception("error page render failed for %s", code)
        return fallback


def handle_http_error(e: HTTPException):
    if e.code is None or e.code < 400 or _wants_json():
        return e
    return _page(e.code, e)


def handle_server_error(e: InternalServerError):
    if _wants_json():
        return e
    return _page(500, e)


def register(app: Flask) -> None:
    app.register_error_handler(HTTPException, handle_http_error)
    app.register_error_handler(InternalServerError, handle_server_error)
