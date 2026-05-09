"""
安全工具：URL 重定向校验 + .env 注入防护
==========================================

无业务逻辑，可独立单元测试（safe_next_url 需要 Flask app context 才能调用 url_for，
但函数本身行为是纯的）。
"""
from __future__ import annotations

from flask import url_for


def safe_next_url(candidate: str) -> str:
    """
    校验重定向目标，防止开放重定向（Open Redirect）攻击。

    规则
    ----
    只允许同源相对路径：必须以 "/" 开头，且不以 "//" 开头。
    - "/dashboard"         → ✅ 合法相对路径
    - "//evil.com/phish"   → ❌ 协议相对 URL，仍指向外部域名
    - "https://evil.com"   → ❌ 绝对 URL，指向外部域名
    - "javascript:alert()" → ❌ 非路径，不以 "/" 开头

    login_required 装饰器通过 next=request.path 注入，request.path
    始终是纯路径（以 "/" 开头，不含 host），是安全来源；本函数用于
    校验来自表单/查询参数（不可信来源）的 next 值。

    Parameters
    ----------
    candidate : 从请求参数中读取的原始 next 字符串

    Returns
    -------
    校验通过的路径原样返回；不通过时返回 "/" 首页路径
    """
    if candidate and candidate.startswith("/") and not candidate.startswith("//"):
        return candidate
    return url_for("index")


def sanitize_dotenv(value: str) -> str:
    """剥离换行符，防止 .env 注入攻击（\\n 可伪造新键）。"""
    return value.replace("\r", "").replace("\n", "")
