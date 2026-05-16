"""
API v1 统一响应壳
=================

iOS / 第三方客户端拿到的所有 ``/api/v1/*`` 响应都遵循同一壳形：

成功：
    { "ok": true,  "data": <任意 JSON> }

失败：
    { "ok": false, "error": { "code": "<machine>", "message": "<human>" } }

错误码 (code) 是机器可读的稳定 string，message 给人看。客户端按 code 分支，
不要拿 message 做控制流。

设计原则
--------
- HTTP 状态码与 code 一致：401↔unauthorized、403↔forbidden、404↔not_found、
  400↔validation、429↔rate_limited、500↔server_error。
- 永远不要把后端异常 ``str(e)`` 直接塞 message——可能泄漏路径/SQL/堆栈。
  err_server_error() 写日志但返回固定文案。
"""

from __future__ import annotations

import logging
from typing import Any

from flask import jsonify

logger = logging.getLogger(__name__)


def ok(data: Any = None, status: int = 200):
    """成功响应。data=None 时返回 ``{"ok": true, "data": null}``。"""
    return jsonify({"ok": True, "data": data}), status


def err(code: str, message: str, status: int):
    """通用错误。"""
    return jsonify({
        "ok": False,
        "error": {"code": code, "message": message},
    }), status


# ── 语义化快捷函数 ─────────────────────────────────────────────────

def err_unauthorized(message: str = "未登录或登录已过期"):
    return err("unauthorized", message, 401)


def err_forbidden(message: str = "没有权限"):
    return err("forbidden", message, 403)


def err_not_found(message: str = "资源不存在"):
    return err("not_found", message, 404)


def err_validation(message: str = "参数无效"):
    return err("validation", message, 400)


def err_conflict(message: str = "资源已存在"):
    return err("conflict", message, 409)


def err_rate_limited(message: str = "请求过于频繁，请稍后再试"):
    return err("rate_limited", message, 429)


def err_server_error(exc: Exception | None = None,
                     message: str = "服务器内部错误"):
    """500——异常详情只进日志，不上报客户端。"""
    if exc is not None:
        logger.exception("API server_error: %s", exc)
    return err("server_error", message, 500)
