"""
路由：Resend Inbound Email webhook（email.received）
=====================================================

接收 Resend 入站邮件回调。Webhook payload 只含元数据（from / to / subject /
email_id / attachments 元数据），**不含正文 / headers / 附件二进制**——拿
完整内容要用 ``email_id`` 反查 Resend API。

挂载的 endpoint
- POST /api/inbound/email   → resend_inbound

签名验证
--------
Resend 走 Svix 协议。请求头有三个：
  - ``svix-id``         事件唯一 ID
  - ``svix-timestamp``  Unix 秒时间戳
  - ``svix-signature``  ``v1,<base64>`` 形式，可空格分隔多个轮换签名

校验过程：
  signed = f"{svix_id}.{svix_timestamp}.{raw_body}".encode()
  expected = base64(HMAC-SHA256(secret_bytes, signed))
  任一 v1,xxx 匹配即通过

secret 形如 ``whsec_<base64>``，**前缀 6 字符要去掉再 base64 解码**才是 HMAC
真正用的 bytes。这点 Svix 文档容易踩。

时间窗校验：±5 min（防重放）。

依赖
----
- ``Storage.add_web_notification`` 写 admin 通知面板
- ``curl_cffi.requests`` 反查 Resend API（项目已经在用，没引入新依赖）
- ``config.get_impersonate`` TLS 指纹（保持和 ResendNotifier 一致）
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time
from typing import Any, Optional

import curl_cffi.requests as req
from flask import Flask, Response, jsonify, request

from app.db import storage
from config import get_impersonate

logger = logging.getLogger(__name__)


# Svix 时间戳容忍窗口（秒）。Resend 文档默认 5 min，再大就给重放攻击留窗口。
_SVIX_TOLERANCE_SEC = 5 * 60


def _decode_secret(secret: str) -> Optional[bytes]:
    """
    ``whsec_<base64>`` → 原始字节。

    去掉 ``whsec_`` 前缀后再 base64 解码——直接拿整串编码会错。
    错误的 secret 返回 None，让调用方走 401 路径。
    """
    if not secret:
        return None
    raw = secret[6:] if secret.startswith("whsec_") else secret
    try:
        return base64.b64decode(raw)
    except Exception:
        return None


def _verify_svix(raw_body: bytes, headers: dict[str, str], secret: str) -> bool:
    """
    Svix webhook 签名验证。三步：

    1. 三个 svix-* 头任一缺失 → False
    2. 时间戳超出 ±5 min 容忍窗口 → False（防重放）
    3. 用 ``svix-id``.``svix-timestamp``.``raw_body`` 计算 HMAC-SHA256，
       与 ``svix-signature`` 里任一 ``v1,<base64>`` 比对

    所有比较都用 ``hmac.compare_digest`` 防时序攻击。
    """
    svix_id = headers.get("svix-id", "").strip()
    svix_ts = headers.get("svix-timestamp", "").strip()
    svix_sig = headers.get("svix-signature", "").strip()
    if not (svix_id and svix_ts and svix_sig):
        return False

    secret_bytes = _decode_secret(secret)
    if not secret_bytes:
        return False

    # 时间窗
    try:
        ts = int(svix_ts)
    except ValueError:
        return False
    now = int(time.time())
    if abs(now - ts) > _SVIX_TOLERANCE_SEC:
        return False

    # 计算期望签名
    signed = f"{svix_id}.{svix_ts}.".encode("utf-8") + raw_body
    expected = base64.b64encode(
        hmac.new(secret_bytes, signed, hashlib.sha256).digest()
    ).decode("ascii")

    # svix-signature 形如 "v1,xxx v1,yyy"，任一匹配即可
    for part in svix_sig.split():
        if not part.startswith("v1,"):
            continue
        candidate = part[3:]
        if hmac.compare_digest(candidate, expected):
            return True
    return False


def _fetch_full_email(email_id: str, api_key: str) -> Optional[dict]:
    """
    用 ``email_id`` 反查 Resend API 拿完整邮件（text / html / headers / raw
    download URL / attachment 详情）。

    Resend 自己存档邮件——即便 webhook 时拉失败，以后还能补拉。所以这里
    任何异常都吞掉返回 None，不阻塞 webhook 200 OK（避免 Resend 反复重投）。
    """
    if not email_id or not api_key:
        return None
    try:
        with req.Session(impersonate=get_impersonate()) as session:
            resp = session.get(
                f"https://api.resend.com/emails/receiving/{email_id}",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "User-Agent": "FlatRadar-Inbound/1.0",
                },
                timeout=10,
            )
        if not resp.ok:
            logger.warning(
                "Resend 拉取入站邮件失败 email_id=%s status=%d body=%r",
                email_id, resp.status_code, resp.text[:200],
            )
            return None
        return resp.json()
    except Exception as e:
        logger.exception("Resend 拉取入站邮件异常 email_id=%s: %s", email_id, e)
        return None


def _is_dmarc_report(sender: str, subject: str) -> bool:
    """
    粗判：是不是 DMARC 聚合报告。
    DMARC 报告 sender 多为 ``noreply-dmarc-support@google.com`` 或
    ``dmarc@*`` / ``postmaster@*``，subject 含 "Report domain"。
    """
    sender_lc = sender.lower()
    subject_lc = subject.lower()
    return (
        "dmarc" in sender_lc
        or "report domain" in subject_lc
        or subject_lc.startswith("report-id:")
    )


def _forward_inbound(
    email_id: str,
    sender: str,
    subject: str,
    text_body: str,
    html_body: str,
    is_dmarc: bool,
) -> None:
    """
    把入站邮件转发到管理员个人邮箱（INBOUND_FORWARD_TO）。

    仅转发非 DMARC 的真实邮件；转发失败只记日志不阻塞 webhook 200。
    """
    forward_to = os.environ.get("INBOUND_FORWARD_TO", "").strip()
    if not forward_to:
        return
    if is_dmarc:
        return  # DMARC 报告不需要转发

    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    from_addr = os.environ.get("RESEND_FROM", "").strip()
    if not api_key or not from_addr:
        logger.warning("转发入站邮件跳过: Resend 未配置")
        return

    fwd_subject = f"Fwd: {subject}" if not subject.startswith("Fwd:") else subject

    # 构建转发正文
    fwd_text = (
        f"---------- 转发的邮件 ----------\n"
        f"发件人: {sender}\n"
        f"收件人: notify@flatradar.app\n"
        f"主题: {subject}\n\n"
        f"{text_body or '(无文字内容)'}"
    )

    # 原始 HTML 包裹转发头
    if html_body:
        fwd_html = (
            f'<div style="color:#6b7280;font-size:13px;margin-bottom:12px;'
            f'padding-bottom:12px;border-bottom:1px solid #e5e7eb;">'
            f'<strong>转发的邮件</strong><br>'
            f'发件人: {html_body_escape(sender)}<br>'
            f'收件人: notify@flatradar.app<br>'
            f'主题: {html_body_escape(subject)}'
            f'</div>'
            f'{html_body}'
        )
    else:
        fwd_html = ""

    payload = {
        "from": from_addr,
        "to": [forward_to],
        "subject": fwd_subject,
        "text": fwd_text,
        **(dict(html=fwd_html) if fwd_html else {}),
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        with req.Session(impersonate=get_impersonate()) as session:
            r = session.post(
                "https://api.resend.com/emails",
                json=payload,
                headers=headers,
                timeout=15,
            )
    except Exception as e:
        logger.error("转发入站邮件网络错误 email_id=%s: %s", email_id, e)
        return

    if 200 <= r.status_code < 300:
        logger.info("📤 入站邮件已转发 email_id=%s → %s", email_id, forward_to)
    else:
        logger.error(
            "转发入站邮件失败 email_id=%s status=%s body=%s",
            email_id, r.status_code, (r.text or "")[:200],
        )


def html_body_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def resend_inbound() -> Any:
    """
    POST /api/inbound/email

    Resend ``email.received`` webhook 回调。payload 形如::

        {
          "type": "email.received",
          "created_at": "2026-02-22T23:41:12.126Z",
          "data": {
            "email_id": "56761188-...",
            "from": "user@example.com",
            "to": ["notify@flatradar.app"],
            "subject": "...",
            "attachments": [{"id":"...","filename":"...","content_type":"..."}]
          }
        }
    """
    # cache=True：让 Flask 缓存原始 body，后面我们要自己 parse JSON（不能用
    # request.get_json，因为已经手动读过 raw bytes 做签名校验了）。
    raw = request.get_data(cache=True)
    secret = os.environ.get("RESEND_WEBHOOK_SECRET", "").strip()
    if not secret:
        logger.error("RESEND_WEBHOOK_SECRET 未配置——拒绝任何 webhook 请求")
        return Response("server not configured", status=503)

    # Flask Headers 不区分大小写，但 .get() 默认大小写敏感——用 lower 化为 dict
    headers = {k.lower(): v for k, v in request.headers.items()}
    if not _verify_svix(raw, headers, secret):
        logger.warning(
            "Resend webhook 签名验证失败 ip=%s svix-id=%r",
            request.headers.get("X-Forwarded-For", request.remote_addr),
            headers.get("svix-id", "")[:40],
        )
        return Response("invalid signature", status=401)

    # 从已读过的 raw bytes 自己 parse——不复用 request.get_json，避免 body
    # 已经被消费一次的歧义。
    import json as _json
    try:
        payload = _json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError):
        payload = {}
    event_type = payload.get("type", "")
    data = payload.get("data") or {}

    # 我们这条 webhook 只订阅 email.received，但防御性地兼容多事件投递
    if event_type != "email.received":
        logger.info("Resend webhook 已忽略事件 type=%s", event_type)
        return jsonify(ok=True, skipped=event_type)

    email_id = str(data.get("email_id") or "").strip()
    sender = str(data.get("from") or "").strip()
    recipients = [r for r in (data.get("to") or []) if isinstance(r, str)]
    subject = str(data.get("subject") or "")
    attachments = data.get("attachments") or []

    logger.info(
        "📥 inbound email_id=%s from=%s to=%s subject=%r attachments=%d",
        email_id, sender, recipients, subject[:80], len(attachments),
    )

    # 反查完整邮件（拉失败不影响 webhook 200，Resend 自己存档了）
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    full = _fetch_full_email(email_id, api_key)
    text_body = ""
    html_body = ""
    if full:
        text_body = (full.get("text") or "")[:5000]
        html_body = (full.get("html") or "")[:10000]

    is_dmarc = _is_dmarc_report(sender, subject)

    # admin 通知面板入库
    # DMARC 报告每天会来一堆，type 单独区分，admin UI 可以日后过滤掉降噪
    notif_type = "inbound_dmarc" if is_dmarc else "inbound_email"
    notif_title = (
        f"📊 DMARC 报告 · {sender}" if is_dmarc
        else f"📥 收信: {subject[:60]}"
    )
    notif_body_parts = [
        f"From: {sender}",
        f"To: {', '.join(recipients) if recipients else '—'}",
    ]
    if attachments:
        names = [a.get("filename", "?") for a in attachments if isinstance(a, dict)]
        notif_body_parts.append(f"Attachments: {', '.join(names)}")
    if text_body and not is_dmarc:
        notif_body_parts.append("")
        notif_body_parts.append(text_body[:1500])

    st = storage()
    try:
        st.add_web_notification(
            type=notif_type,
            title=notif_title,
            body="\n".join(notif_body_parts),
        )
    except Exception:
        # 入库失败不能让 Resend 重投——已经记 INFO 日志了
        logger.exception("入站邮件写 web_notifications 失败 email_id=%s", email_id)
    finally:
        st.close()

    # 转发到管理员个人邮箱
    _forward_inbound(email_id, sender, subject, text_body, html_body, is_dmarc)

    return jsonify(ok=True, email_id=email_id)


def register(app: Flask) -> None:
    """
    挂载 inbound webhook。
    不走 @csrf_required——外部 webhook 不带 CSRF token；安全靠 Svix 签名。
    """
    app.add_url_rule(
        "/api/inbound/email",
        endpoint="resend_inbound",
        view_func=resend_inbound,
        methods=["POST"],
    )
