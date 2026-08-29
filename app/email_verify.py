"""收件邮箱归属验证 — 发链接 + 处理点击。

设计要点
--------
- 仅 shared 模式（Resend）需要：把"邮件能进到 admin 的发件域"和
  "用户对收件邮箱有控制权"分开，防止 user 把别人邮箱填进来当代发服务
- 验证邮件本身也用 Resend 发：链路一致；admin 没配 Resend 则无法验证，
  此时 shared 模式整体不可用（fail-closed）
- 链接形如 ``PUBLIC_BASE_URL/verify-email/<token>``。PUBLIC_BASE_URL 必须
  显式配置，避免 Host header 注入影响邮件链接。
"""
from __future__ import annotations

import asyncio
import logging
import os
from urllib.parse import urlparse

from app.db import storage
from notifier import ResendNotifier, get_shared_email_config

logger = logging.getLogger(__name__)


class EmailVerifyConfigError(RuntimeError):
    """邮箱验证功能缺少必要生产配置，属于可预期配置错误。"""


def _build_verify_url(token: str) -> str:
    """
    用 PUBLIC_BASE_URL 生成验证链接。

    不再 fallback 到 request.host_url：邮件验证链接是安全边界，Host header
    在反代/开发服务器配置不严时可被客户端控制，不能参与 token URL 生成。
    """
    base = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if not base:
        logger.error("PUBLIC_BASE_URL 未配置，拒绝生成邮箱验证链接")
        raise EmailVerifyConfigError("PUBLIC_BASE_URL 未配置")
    parsed = urlparse(base)
    if parsed.scheme != "https" or not parsed.netloc:
        logger.error("PUBLIC_BASE_URL 必须是 https 绝对 URL: %r", base)
        raise EmailVerifyConfigError("PUBLIC_BASE_URL 配置无效")
    return f"{base}/verify-email/{token}"


def _html_escape(s: str) -> str:
    """最小 HTML 转义。不引入 markupsafe 依赖；只覆盖 user_name 这一变量来源。"""
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
         .replace("'", "&#39;")
    )


def _safe_user_name(raw: str) -> str:
    """
    输出层兜底：剥换行 / 控制字符 / 零宽字符。

    输入层 (app/forms/user_form.py:_sanitize_display_name) 已对新提交的 name 做了
    同样清洗，这里二次过滤是为了处理升级前已经写入数据库的脏数据，
    避免它们经邮件正文构造伪段落（社工攻击 / 钓鱼）。
    """
    if not raw:
        return ""
    bad = set(chr(c) for c in range(0x00, 0x20))
    bad |= {"\x7f", "\u2028", "\u2029", "\u200b", "\u200c", "\u200d", "\ufeff"}
    cleaned = "".join(" " if c in bad else c for c in raw)
    return " ".join(cleaned.split())[:64]


#: 验证邮件的中英两份文案。
#:
#: 此前整封信写死中文，而 ``UserConfig.language`` 默认就是 ``en``——2026-08-29
#: 线上 62 个用户全部是 en，配了邮箱的 13 个也全部是 en。也就是说至今发出去的
#: 每一封验证邮件都是中文发给英文用户的。房源通知一直是按 ``user.language``
#: 分支的，只有这一封没跟上。
#:
#: 落款那句沿用 notifier.py 里的写法，两种语言相同：品牌行不翻译，而且项目早已
#: 不只监控 Holland2Stay，原文「Holland2Stay 房源监控」是四个平台里的一个。
_VERIFY_TEXT = {
    "zh": {
        "html_lang":  "zh",
        "subject":    "FlatRadar — 确认你的通知邮箱",
        "title":      "确认你的通知邮箱",
        "intro":      "你好 {name}，有人（很可能是你本人）把这个邮箱地址设为 "
                      "FlatRadar 的通知收件人。为防止他人滥用，请点击下方按钮确认这是你的邮箱。",
        "button":     "确认邮箱",
        "fallback":   "如按钮无法点击，复制以下链接到浏览器打开：",
        "expiry":     "链接 24 小时内有效。如果你不知道这封邮件是什么，请直接忽略——"
                      "未确认的邮箱不会收到任何后续通知，也不会再收到本类邮件。",
        "text_body":  "你好 {name},\n\n"
                      "有人（很可能是你本人）把这个邮箱地址设为 FlatRadar 的通知收件人。\n"
                      "为防止他人滥用，请点击以下链接确认这是你的邮箱：\n\n"
                      "{url}\n\n"
                      "链接 24 小时内有效。如果你不知道这封邮件是什么，请直接忽略——\n"
                      "未确认的邮箱不会收到任何后续通知。\n\n"
                      "— FlatRadar",
    },
    "en": {
        "html_lang":  "en",
        "subject":    "FlatRadar — Confirm your notification email",
        "title":      "Confirm your notification email",
        "intro":      "Hi {name}, someone (most likely you) set this address as the "
                      "notification recipient for FlatRadar. To stop anyone else from "
                      "using it, please confirm the address with the button below.",
        "button":     "Confirm email",
        "fallback":   "If the button does not work, copy this link into your browser:",
        "expiry":     "The link is valid for 24 hours. If you were not expecting this "
                      "email, just ignore it — an unconfirmed address receives no "
                      "notifications, and no further messages of this kind.",
        "text_body":  "Hi {name},\n\n"
                      "Someone (most likely you) set this address as the notification "
                      "recipient for FlatRadar.\n"
                      "To stop anyone else from using it, confirm the address here:\n\n"
                      "{url}\n\n"
                      "The link is valid for 24 hours. If you were not expecting this "
                      "email, just ignore it —\n"
                      "an unconfirmed address receives no notifications.\n\n"
                      "— FlatRadar",
    },
}

#: 页脚。与 notifier.py 的通知邮件同一句，不随语言变化。
_VERIFY_FOOTER = "© FlatRadar · independent rental listing companion"


def _format_verify_email(
    verify_url: str, user_name: str, lang: str = "en",
) -> tuple[str, str, str]:
    """
    返回 (subject, text_body, html_body)。

    - text 是 fallback：纯客户端 / 邮件助读器优先用 text
    - html 用 inline style + 表格布局，最大化 Gmail / Outlook 兼容性：
      Gmail 会剥掉 <style>，所以一切样式必须 inline；外层 table 防止 Outlook
      把内容塞进窄列。
    - user_name 同时做 (1) 控制字符脱敏 → text，(2) HTML 实体转义 → html
    - lang 取自 ``UserConfig.language``，与房源通知同一个字段；认不出的值按 en。

    ``_VERIFY_TEXT`` 里的文案是本文件自己写的常量，不含任何 HTML 元字符，直接
    插进模板即可；唯一需要转义的是 ``user_name``，而它在 ``safe_name`` 那一步
    已经转义过一次。外面再包一层 ``_html_escape`` 会把 ``&`` 变成 ``&amp;amp;``
    ——名字里有 ``&`` 的人会在邮件里看到实体码。有一条用例钉住文案不含元字符。
    """
    user_name = _safe_user_name(user_name)
    t = _VERIFY_TEXT.get((lang or "en").lower()[:2], _VERIFY_TEXT["en"])
    subject = t["subject"]
    text_body = t["text_body"].format(name=user_name, url=verify_url)

    safe_name = _html_escape(user_name or "")
    safe_url = _html_escape(verify_url)
    html_body = f"""<!doctype html>
<html lang="{t["html_lang"]}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{t["title"]}</title>
</head>
<body style="margin:0;padding:0;background:#f5f7fa;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;color:#1f2530;">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#f5f7fa;padding:32px 16px;">
  <tr>
    <td align="center">
      <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="520"
             style="max-width:520px;width:100%;background:#ffffff;border-radius:14px;
                    box-shadow:0 1px 3px rgba(20,30,50,.06);overflow:hidden;">
        <tr>
          <td style="padding:32px 36px 12px;">
            <div style="font-size:13px;font-weight:600;letter-spacing:.5px;color:#0f6b7a;">
              FLATRADAR
            </div>
            <h1 style="margin:14px 0 8px;font-size:22px;font-weight:600;line-height:1.3;color:#1f2530;">
              {t["title"]}
            </h1>
            <p style="margin:0;color:#6b7280;font-size:14px;line-height:1.6;">
              {t["intro"].format(name=safe_name)}
            </p>
          </td>
        </tr>
        <tr>
          <td align="center" style="padding:24px 36px 8px;">
            <a href="{safe_url}"
               style="display:inline-block;padding:12px 28px;background:#0f6b7a;color:#ffffff;
                      text-decoration:none;border-radius:8px;font-size:15px;font-weight:500;
                      box-shadow:0 1px 2px rgba(15,107,122,.3);">
              {t["button"]}
            </a>
          </td>
        </tr>
        <tr>
          <td style="padding:8px 36px 4px;">
            <p style="margin:0;color:#9aa3ad;font-size:12px;line-height:1.6;text-align:center;">
              {t["fallback"]}
            </p>
            <p style="margin:8px 0 0;word-break:break-all;text-align:center;">
              <a href="{safe_url}" style="color:#0f6b7a;font-size:12px;text-decoration:none;">{safe_url}</a>
            </p>
          </td>
        </tr>
        <tr>
          <td style="padding:24px 36px 32px;border-top:1px solid #eef0f3;margin-top:24px;">
            <p style="margin:18px 0 0;color:#9aa3ad;font-size:12px;line-height:1.6;">
              {t["expiry"]}
            </p>
          </td>
        </tr>
      </table>
      <p style="margin:14px 0 0;color:#b8c0cc;font-size:11px;">
        {_VERIFY_FOOTER}
      </p>
    </td>
  </tr>
</table>
</body>
</html>"""
    return subject, text_body, html_body


async def send_verification_email(
    user_id: str, user_name: str, email: str, lang: str = "en",
) -> bool:
    """
    给指定邮箱发一封带 token 链接的验证邮件。

    返回 True 表示已成功投递给 Resend API（不代表对方收到，但已尽力）。
    返回 False 表示没发出去（Resend 未配置 / API 失败），调用方应给用户提示。

    安全性：每次调用都新建 token；不限制单 user 每邮箱多少次（重发场景）。
    每日总配额由调用方上层限流（test_notify 限流 + 后续 P4 全局配额）。
    """
    ok, api_key, from_addr = get_shared_email_config()
    if not ok:
        logger.warning("跳过邮箱验证：Resend 未配置")
        return False

    st = storage()
    try:
        token = st.create_email_verification(user_id, email)
    finally:
        st.close()

    verify_url = _build_verify_url(token)
    subject, text_body, html_body = _format_verify_email(verify_url, user_name, lang)

    # 验证邮件需要自定义 subject 和确认按钮链接，因此直接调底层 HTTP
    # （与 ResendNotifier._post 同形）。配额由该函数自己调用 check/record
    # （验证邮件不归属任何 user_id，只占全局额度）。
    from notifier import check_resend_quota, record_resend_send
    ok_quota, reason = check_resend_quota("")
    if not ok_quota:
        logger.warning("验证邮件被配额拒发: %s", reason)
        return False

    import curl_cffi.requests as req

    from net import direct_curl_session
    from config import get_impersonate

    payload = {
        "from": from_addr,
        "to": [email],
        "subject": subject,
        "text": text_body,
        "html": html_body,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    def _post() -> bool:
        try:
            with direct_curl_session(impersonate=get_impersonate()) as s:
                r = s.post(ResendNotifier.ENDPOINT, json=payload,
                           headers=headers, timeout=15)
        except Exception as e:
            logger.error("验证邮件 Resend 网络错误: %s", e)
            return False
        if 200 <= r.status_code < 300:
            return True
        logger.error(
            "验证邮件 Resend 失败 status=%s body=%s",
            r.status_code, (r.text or "")[:300],
        )
        return False

    loop = asyncio.get_running_loop()
    sent = await loop.run_in_executor(None, _post)
    if sent:
        record_resend_send("")
    return sent


def send_verification_email_sync(
    user_id: str, user_name: str, email: str, lang: str = "en",
) -> bool:
    """同步包装，给 Flask 路由层调用（避免每个路由都管 event loop）。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        has_running_loop = False
    else:
        has_running_loop = True

    if not has_running_loop:
        return asyncio.run(send_verification_email(user_id, user_name, email, lang))

    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(
            asyncio.run, send_verification_email(user_id, user_name, email, lang)
        ).result()
