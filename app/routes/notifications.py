"""
路由：Web 通知（分页查询 + 标记已读 + SSE 推送）

挂载的 endpoint
- GET  /api/notifications        → api_notifications
- POST /api/notifications/read   → api_notifications_read
- GET  /api/events               → api_events（SSE，长连接）
"""
from __future__ import annotations

import json
import threading
import time as _time

from flask import Flask, Response, jsonify, request, stream_with_context

from app.auth import admin_api_required
from app.csrf import csrf_required
from app.db import storage


@admin_api_required
def api_notifications():
    """
    分页查询 Web 通知列表。

    Query params
    ------------
    limit  : 每页条数，默认 50，最大 200
    offset : 分页偏移，默认 0
    """
    try:
        limit  = min(int(request.args.get("limit",  50)), 200)
        offset = max(int(request.args.get("offset",  0)),   0)
    except (TypeError, ValueError):
        limit, offset = 50, 0

    st = storage()
    try:
        rows   = st.get_notifications(limit=limit, offset=offset)
        unread = st.count_unread_notifications()
    finally:
        st.close()

    return jsonify({"ok": True, "notifications": rows, "unread": unread})


@admin_api_required
@csrf_required
def api_notifications_read():
    """标记所有通知为已读（或指定 ids 数组）。"""
    data = request.get_json(silent=True) or {}
    ids  = data.get("ids")  # None → 全部；list[int] → 指定
    if ids is not None:
        if not isinstance(ids, list):
            return jsonify({"ok": False, "error": "ids 必须是数组"}), 400
        try:
            ids = [int(i) for i in ids]
        except (ValueError, TypeError):
            return jsonify({"ok": False, "error": "ids 元素必须是整数"}), 400

    st = storage()
    try:
        st.mark_notifications_read(ids=ids)
    finally:
        st.close()

    return jsonify({"ok": True})


@admin_api_required
def api_events():
    """
    SSE（Server-Sent Events）端点，每 5 秒推送增量通知给浏览器。

    浏览器通过 EventSource('/api/events?last_id=N') 订阅。
    若有新通知，发送 `data: <JSON数组>\\n\\n`；否则发送保活注释 `: keepalive\\n\\n`。

    线程泄漏防护（三层）
    --------------------
    Gunicorn sync worker 在客户端断连后不一定向生成器注入 GeneratorExit——
    生成器阻塞在 time.sleep() 期间无法接收任何信号，线程因此永久堆积。

    1. **yield 处捕获写入异常**
       BrokenPipeError / ConnectionResetError / OSError 表示客户端已断连，
       任一异常立即 return，不再进入下一轮循环。

    2. **可中断 sleep（threading.Event.wait）**
       用 stop.wait(5) 替代 time.sleep(5)：
       当写入异常退出时，finally 块调用 stop.set()，
       若此时另一个线程（或同一线程的其他路径）正在 wait()，立即唤醒。

    3. **硬性最大连接时长（_SSE_MAXAGE = 300 s）**
       到期后主动关闭连接；浏览器 EventSource 按 `retry: 2000`（2 s）
       自动重连，对用户完全透明。

    备注：SSE 路由不走 app.db.storage 请求作用域（teardown_request 会在
    流式响应启动后立即关闭），所以此处显式新建 + 关闭一个 Storage 实例。

    Gunicorn 推荐部署
    -----------------
    gevent/eventlet worker 可协作式处理断连，无需以上防护：
        gunicorn -k gevent --worker-connections 1000 web:app
    """
    _SSE_POLL   = 5    # 轮询间隔（秒）
    _SSE_MAXAGE = 300  # 单次连接最大生命周期（秒），到期后让浏览器重连

    try:
        last_id = int(request.args.get("last_id", 0))
    except (TypeError, ValueError):
        last_id = 0

    stop    = threading.Event()
    expires = _time.monotonic() + _SSE_MAXAGE

    def _generate():
        nonlocal last_id
        # 告知浏览器 2 s 后重连（连接到期或异常关闭时生效）
        yield "retry: 2000\n\n"
        st = storage()
        try:
            while not stop.is_set() and _time.monotonic() < expires:
                rows = st.get_notifications_since(last_id)

                if rows:
                    last_id = rows[-1]["id"]
                    payload = json.dumps(rows, ensure_ascii=False)
                    chunk = f"data: {payload}\n\n"
                else:
                    chunk = ": keepalive\n\n"

                try:
                    yield chunk
                except (GeneratorExit, BrokenPipeError, ConnectionResetError, OSError):
                    # 写入失败 = 客户端已断连，立即退出，不再轮询
                    return

                # 可中断 sleep：stop.set() 后立即唤醒，无需等满 _SSE_POLL 秒
                stop.wait(_SSE_POLL)
        except GeneratorExit:
            pass
        finally:
            st.close()
            stop.set()  # 确保所有退出路径都能唤醒任何正在等待的 stop.wait()

    return Response(
        stream_with_context(_generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # 禁用 Nginx 缓冲，确保实时推送
            "Connection": "keep-alive",
        },
    )


def register(app: Flask) -> None:
    app.add_url_rule("/api/notifications",      endpoint="api_notifications",      view_func=api_notifications,      methods=["GET"])
    app.add_url_rule("/api/notifications/read", endpoint="api_notifications_read", view_func=api_notifications_read, methods=["POST"])
    app.add_url_rule("/api/events",             endpoint="api_events",             view_func=api_events,             methods=["GET"])
