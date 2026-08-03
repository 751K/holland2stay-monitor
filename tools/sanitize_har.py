"""tools/sanitize_har.py — 清洗浏览器导出的 HAR，去掉凭据后再交给别人分析

为什么需要它
------------
侦察 RENTCafe 登录之后的表单流程，最省事的办法是让管理员在浏览器里手动走一遍
并导出 HAR。**但 HAR 会原样记录密码和会话 cookie**——登录那个 POST 的
request body 里就是明文密码，之后每个请求都带着能直接冒充你的 session cookie。

所以 HAR 不能原样交出去。这个脚本把敏感部分抹掉，只留下侦察真正需要的东西：
URL 序列、表单字段名、非敏感字段值、以及响应 HTML。

用法::

    python -m tools.sanitize_har raw.har -o clean.har
    python -m tools.sanitize_har raw.har -o clean.har --host securerc.co.uk

抹掉什么
--------
- 所有 Cookie（请求的 Cookie 头、响应的 Set-Cookie、以及 HAR 的 cookies 数组）
- Authorization / X-CSRF / API key 之类的头
- 名字像密码/令牌的表单字段与查询参数（见 ``_SENSITIVE_NAMES``）
- 响应体里出现的 g-recaptcha token（很长且无复用价值）

保留什么
--------
URL、HTTP 方法、状态码、时间、请求头（去敏后）、表单**字段名**、非敏感字段值、
响应 HTML。这些就足以还原「第几步 POST 到哪、带了哪些字段」。

> 这是尽力而为的清洗，不是安全保证。交出去之前**自己再扫一眼**输出文件——
> 站点可能把敏感信息放在这里没预料到的字段名里。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

REDACTED = "[REDACTED]"

# 字段名匹配到这些就抹掉值（大小写不敏感，子串匹配）
_SENSITIVE_NAMES = (
    "password", "passwd", "pwd", "secret", "token", "auth",
    "ssn", "creditcard", "cardnumber", "cvv", "iban", "bsn",
    "otp", "verificationcode", "securityanswer",
    # 账号标识本身也是 PII。字段**名**照常保留，所以不影响还原表单结构。
    "username", "useremail", "emailaddress",
    # reCAPTCHA token：一次性、两分钟过期、几百字符纯噪音。
    # 特意写全 "recaptcha-response" 而不是 "captcha"——后者会连
    # failed-captcha-3-rentable 一起抹掉，而那个字段的 true/false 是关键信号。
    "recaptcha-response",
)

# 这些请求头整个抹掉
_SENSITIVE_HEADERS = (
    "cookie", "set-cookie", "authorization", "proxy-authorization",
    "x-csrf-token", "x-xsrf-token", "x-api-key",
)

# 响应体里的 reCAPTCHA token：很长、一次性、留着没用
_TOKEN_RE = re.compile(r"[0-9A-Za-z_\-]{200,}")


def _is_sensitive(name: str) -> bool:
    n = (name or "").lower()
    return any(k in n for k in _SENSITIVE_NAMES)


def _clean_nv_list(items: list[dict] | None) -> list[dict]:
    """清洗 HAR 的 name/value 数组（headers / queryString / params）。"""
    out = []
    for it in items or []:
        name = it.get("name", "")
        if name.lower() in _SENSITIVE_HEADERS or _is_sensitive(name):
            it = {**it, "value": REDACTED}
        out.append(it)
    return out


def _clean_post(post: dict | None) -> dict | None:
    if not post:
        return post
    post = dict(post)
    post["params"] = _clean_nv_list(post.get("params"))

    text = post.get("text")
    if isinstance(text, str) and text:
        # x-www-form-urlencoded：逐个字段判断
        if "=" in text and "\n" not in text[:200]:
            parts = []
            for kv in text.split("&"):
                k, sep, v = kv.partition("=")
                parts.append(f"{k}{sep}{REDACTED}" if _is_sensitive(k) else kv)
            text = "&".join(parts)
        # JSON body：递归抹
        elif text.lstrip().startswith(("{", "[")):
            try:
                text = json.dumps(_clean_json(json.loads(text)), ensure_ascii=False)
            except Exception:
                pass
        post["text"] = _TOKEN_RE.sub(REDACTED, text)
    return post


def _clean_json(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: (REDACTED if _is_sensitive(k) else _clean_json(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_clean_json(v) for v in obj]
    return obj


def sanitize(har: dict, *, host_filter: str = "", keep_bodies: bool = True) -> dict:
    entries = har.get("log", {}).get("entries", [])
    cleaned = []
    for e in entries:
        req = e.get("request", {}) or {}
        url = req.get("url", "")
        if host_filter and host_filter not in url:
            continue

        req = dict(req)
        req["headers"] = _clean_nv_list(req.get("headers"))
        req["queryString"] = _clean_nv_list(req.get("queryString"))
        req["cookies"] = []
        req["postData"] = _clean_post(req.get("postData"))

        res = dict(e.get("response", {}) or {})
        res["headers"] = _clean_nv_list(res.get("headers"))
        res["cookies"] = []
        content = dict(res.get("content", {}) or {})
        if keep_bodies and isinstance(content.get("text"), str):
            content["text"] = _TOKEN_RE.sub(REDACTED, content["text"])
        elif not keep_bodies:
            content.pop("text", None)
        res["content"] = content

        cleaned.append({**e, "request": req, "response": res})

    out = json.loads(json.dumps(har))          # 深拷贝，不改原对象
    out["log"]["entries"] = cleaned
    return out


def summarize(har: dict) -> str:
    """人读的流程概览：第几步、什么方法、到哪个 URL、带了哪些字段。"""
    lines = []
    for i, e in enumerate(har.get("log", {}).get("entries", []), 1):
        req = e.get("request", {}) or {}
        res = e.get("response", {}) or {}
        url = req.get("url", "").split("?")[0]
        names = [p.get("name", "") for p in (req.get("postData") or {}).get("params", [])]
        lines.append(
            f"{i:3}. {req.get('method','?'):4} {res.get('status','?')} {url}"
            + (f"\n      字段({len(names)}): {', '.join(names[:25])}" if names else "")
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="清洗 HAR，去掉凭据后再分享")
    ap.add_argument("input", type=Path)
    ap.add_argument("-o", "--output", type=Path, required=True)
    ap.add_argument("--host", default="", help="只保留 URL 含该子串的请求，如 securerc.co.uk")
    ap.add_argument("--no-bodies", action="store_true", help="连响应体一起丢掉（更保守）")
    ap.add_argument("--summary", action="store_true", help="同时打印流程概览")
    a = ap.parse_args(argv)

    har = json.loads(a.input.read_text(encoding="utf-8"))
    out = sanitize(har, host_filter=a.host, keep_bodies=not a.no_bodies)
    a.output.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    n_in = len(har.get("log", {}).get("entries", []))
    n_out = len(out.get("log", {}).get("entries", []))
    print(f"已清洗: {n_in} 条请求 → 保留 {n_out} 条 → {a.output}")
    print("⚠️  交出去之前请自己再扫一眼输出文件——站点可能把敏感信息放在意料之外的字段名里。")
    if a.summary:
        print("\n" + summarize(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
