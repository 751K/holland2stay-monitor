"""
app.safety 模块的安全边界测试。

safe_next_url 是开放重定向防护：只允许同源相对路径。
sanitize_dotenv 阻止 .env 注入（\\n 伪造新键）。

这两个函数都很短，但它们是攻击面。一旦放过 //evil.com 或换行符，
就是 stored XSS / 凭证泄漏的入口。
"""
from __future__ import annotations

import pytest

from app.safety import safe_next_url, sanitize_dotenv


class TestSafeNextUrl:
    """safe_next_url 的核心契约：合法相对路径放行，其他全部回退到 url_for('index')。"""

    def test_relative_path_passes(self, app_ctx):
        assert safe_next_url("/dashboard") == "/dashboard"
        assert safe_next_url("/users/abc123") == "/users/abc123"
        assert safe_next_url("/api/status?foo=bar") == "/api/status?foo=bar"

    def test_empty_falls_back_to_index(self, app_ctx):
        # 空字符串、None 都应该回到首页
        assert safe_next_url("") == "/"
        # None 触发 falsy 分支
        assert safe_next_url(None) == "/"  # type: ignore[arg-type]

    def test_protocol_relative_url_blocked(self, app_ctx):
        """`//evil.com` 是协议相对 URL，指向外部域，必须拦截。"""
        # 浏览器会解释为 https://evil.com/phish；fallback 到 "/"
        assert safe_next_url("//evil.com") == "/"
        assert safe_next_url("//evil.com/phish") == "/"
        # 三个或更多斜杠也应该被认为是 //... 开头
        assert safe_next_url("///etc/passwd") == "/"

    def test_absolute_url_blocked(self, app_ctx):
        """`https://...` / `http://...` 绝对 URL 都拦截。"""
        assert safe_next_url("https://evil.com/phish") == "/"
        assert safe_next_url("http://evil.com") == "/"
        assert safe_next_url("ftp://evil.com") == "/"

    def test_javascript_uri_blocked(self, app_ctx):
        """`javascript:...` 不以 / 开头，应该被拦截。"""
        assert safe_next_url("javascript:alert(1)") == "/"
        assert safe_next_url("data:text/html,<script>") == "/"

    def test_no_leading_slash_blocked(self, app_ctx):
        """相对路径必须以 / 开头。"""
        assert safe_next_url("dashboard") == "/"
        assert safe_next_url("../etc/passwd") == "/"

    def test_returns_string_not_response(self, app_ctx):
        """返回值必须是字符串，不是 Flask Response 对象（调用方需要传给 redirect()）。"""
        assert isinstance(safe_next_url("/x"), str)
        assert isinstance(safe_next_url(""), str)


class TestSanitizeDotenv:
    """sanitize_dotenv 剥离换行符，防止用户输入伪造 .env 新键。"""

    def test_passthrough_normal_value(self):
        """普通字符串原样返回。"""
        assert sanitize_dotenv("hello") == "hello"
        assert sanitize_dotenv("with spaces") == "with spaces"
        assert sanitize_dotenv("special!@#$%^&*()") == "special!@#$%^&*()"

    def test_strips_newline_and_carriage_return(self):
        """\\n 和 \\r 都必须剥离 —— 都能在 .env 注入新键。"""
        # 攻击负载：值字符串里嵌入 \n + 假键 = 写出第二行 ADMIN=true
        attack = "value\nADMIN=true"
        assert "\n" not in sanitize_dotenv(attack)
        assert sanitize_dotenv(attack) == "valueADMIN=true"

        # \r\n 行尾（Windows）
        attack2 = "x\r\nFLASK_SECRET=hijacked"
        assert "\r" not in sanitize_dotenv(attack2)
        assert "\n" not in sanitize_dotenv(attack2)

    def test_empty_string(self):
        assert sanitize_dotenv("") == ""

    def test_multiple_newlines(self):
        """多个换行都要剥光。"""
        assert sanitize_dotenv("a\nb\nc\nd") == "abcd"
        assert sanitize_dotenv("\n\n\n") == ""

    def test_only_strips_newlines_keeps_other_whitespace(self):
        """tab/空格不动 —— 它们不能在 .env 里换键。"""
        assert sanitize_dotenv("a\tb c") == "a\tb c"
