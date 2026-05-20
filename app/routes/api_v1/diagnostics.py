"""
API v1 崩溃诊断端点
====================
POST /api/v1/diagnostics/crash — iOS 客户端在用户授权后上传 MetricKit 收到的
``MXCrashDiagnostic`` / ``MXHangDiagnostic`` JSON 包。

设计要点
--------
- ``bearer_optional``：guest / 未登录用户也能上传（崩溃可能发生在登录前）。
  带 token 时记录 user_id，匿名时只留 IP/UA 元信息
- 数据用 **文件存储**：每条一个 JSON 文件落 ``data/crash_reports/``。
  不进 SQLite —— 诊断 payload 体积不定（可能含完整堆栈、上百帧），
  入库反而拖慢正常查询，且 admin 排查崩溃靠 grep 一搜更直接
- 速率限制：同 IP 每小时最多 20 条，防止有人把崩溃端点当成日志倾倒口
- payload 大小硬上限 256 KB —— 苹果实际 crash 包都 < 64 KB
"""

from __future__ import annotations

import json
import logging
import time as _time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Blueprint, g, request

from app import api_auth, api_errors as _err
from config import DATA_DIR

logger = logging.getLogger(__name__)

CRASH_DIR = DATA_DIR / "crash_reports"
MAX_PAYLOAD_BYTES = 256 * 1024
ALLOWED_KINDS = {"crash", "hang", "diskwrite", "cpuexception"}

# 每 IP 每小时最多上传次数（防滥用）
_RATE_PER_IP_PER_HOUR = 20
_rate_buckets: dict[str, deque] = {}


def _rate_check(ip: str) -> bool:
    """简单滑动窗口：1 小时内同 IP 上传次数限制。返回 True 表示允许。"""
    now = _time.monotonic()
    cutoff = now - 3600
    bucket = _rate_buckets.setdefault(ip, deque())
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= _RATE_PER_IP_PER_HOUR:
        return False
    bucket.append(now)
    return True


def _submit() -> Any:
    """POST /api/v1/diagnostics/crash"""
    client_ip = request.remote_addr or "?"
    if not _rate_check(client_ip):
        return _err.err_rate_limited("诊断上传过频，请稍后再试")

    # 大小预检查（Werkzeug 默认有 16MB 上限，但我们要更紧的）
    raw = request.get_data(cache=False, as_text=False)
    if len(raw) > MAX_PAYLOAD_BYTES:
        return _err.err_validation(
            f"诊断包过大（{len(raw)} bytes，上限 {MAX_PAYLOAD_BYTES}）"
        )

    try:
        body = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _err.err_validation("请求体必须是合法 UTF-8 JSON")

    kind = str(body.get("kind", "crash")).strip().lower()
    if kind not in ALLOWED_KINDS:
        return _err.err_validation(f"kind 必须是 {', '.join(sorted(ALLOWED_KINDS))} 之一")

    payload = body.get("payload")
    if payload is None:
        return _err.err_validation("缺少 payload 字段")

    # 元信息：发件人身份 + 客户端环境
    role = api_auth.current_role() or "anonymous"
    user_id = getattr(g, "api_user_id", None) or ""
    app_version = str(body.get("app_version", ""))[:32]
    ios_version = str(body.get("ios_version", ""))[:32]
    device_model = str(body.get("device_model", ""))[:64]

    received_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    record = {
        "received_at": received_at,
        "kind": kind,
        "role": role,
        "user_id": user_id,
        "client_ip": client_ip,
        "app_version": app_version,
        "ios_version": ios_version,
        "device_model": device_model,
        "user_agent": request.headers.get("User-Agent", "")[:200],
        "payload": payload,
    }

    try:
        CRASH_DIR.mkdir(parents=True, exist_ok=True)
        fname = f"{received_at}-{kind}-{uuid.uuid4().hex[:8]}.json"
        out = CRASH_DIR / fname
        # 原子写入：先写 .tmp，再 rename，避免 admin grep 时撞见半写文件
        tmp = out.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(record, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(out)
    except OSError as e:
        logger.exception("诊断报告写入失败")
        return _err.err_server_error(e, "诊断上传失败，请稍后重试")

    logger.warning(
        "📋 收到崩溃诊断 kind=%s role=%s user=%s app=%s ios=%s device=%s size=%d → %s",
        kind, role, user_id or "-", app_version, ios_version,
        device_model, len(raw), fname,
    )
    return _err.ok({"received": True, "id": fname}, status=202)


def register(bp: Blueprint) -> None:
    bp.add_url_rule(
        "/diagnostics/crash",
        endpoint="diagnostics_crash",
        view_func=api_auth.bearer_optional(_submit),
        methods=["POST"],
    )
