"""
前端辅助函数 + XSS 回归测试。

覆盖：
- _mask_email() 脱敏
- templates 中 XSS 风险点验证（无 innerHTML 拼接用户输入）
- _escape_applescript_literal 完整覆盖（已有 test_applescript_escape.py，此处回归）
"""
from __future__ import annotations

import pytest

from jinja2 import Environment, FileSystemLoader, select_autoescape
from pathlib import Path


# ── _mask_email ────────────────────────────────────────────

class TestMaskEmail:
    def test_normal_email(self):
        from booker import _mask_email
        assert _mask_email("test@example.com") == "tes***@example.com"

    def test_short_local(self):
        from booker import _mask_email
        assert _mask_email("ab@x.com") == "***@x.com"

    def test_empty(self):
        from booker import _mask_email
        assert _mask_email("") == "***"

    def test_no_at_sign(self):
        from booker import _mask_email
        result = _mask_email("noatsign")
        assert "***" in result


# ── Jinja2 自动转义验证 ────────────────────────────────────

class TestTemplateAutoEscape:
    def test_jinja_autoescape_enabled(self):
        """确认 Jinja2 默认自动转义开启（XSS 主要防线）。"""
        env = Environment(
            loader=FileSystemLoader(Path(__file__).parent.parent / "templates"),
            autoescape=select_autoescape(["html"]),
        )
        tmpl = env.from_string("{{ value }}")
        result = tmpl.render(value="<script>alert(1)</script>")
        assert "<script>" not in result
        assert "&lt;script&gt;" in result

    def test_templates_parseable(self):
        """所有模板应可解析（无 Jinja2 语法错误）。"""
        template_dir = Path(__file__).parent.parent / "templates"
        env = Environment(autoescape=select_autoescape(["html"]))
        for path in sorted(template_dir.glob("*.html")):
            source = path.read_text(encoding="utf-8")
            env.parse(source)  # 抛异常 = 语法错误

    def test_users_page_does_not_inline_user_input_in_handlers(self):
        """用户名等注册用户可控字段不能进入 inline JS handler。"""
        source = (Path(__file__).parent.parent / "templates" / "users.html").read_text(
            encoding="utf-8",
        )
        assert "onclick=\"sendTestNotify('{{ user.id }}', '{{ user.name }}'" not in source
        assert "onclick=\"confirmDelete('{{ user.id }}', '{{ user.name }}')" not in source
        assert "data-user-name=\"{{ user.name }}\"" in source

    def test_notification_result_templates_escape_dynamic_errors(self):
        """测试通知结果仍用 innerHTML 布局时，动态错误字段必须先 escape。"""
        source = (Path(__file__).parent.parent / "templates" / "user_form.html").read_text(
            encoding="utf-8",
        )
        assert "+ data.error +" not in source
        assert "+ r.channel +" not in source
        assert "+ r.error +" not in source
        assert "escapeHtml(data.error)" in source
        assert "escapeHtml(r.channel)" in source
        assert "escapeHtml(r.error)" in source


# ── AppleScript 转义回归 ───────────────────────────────────

class TestAppleScriptEscapeRegression:
    def test_backslash_handled_first(self):
        from notifier import _escape_applescript_literal
        result = _escape_applescript_literal('\\"')
        # 反斜杠必须先转义，避免后续双引号转义被反斜杠转义
        assert result == '\\\\\\"'

    def test_newline_becomes_return(self):
        from notifier import _escape_applescript_literal
        result = _escape_applescript_literal("line1\nline2")
        assert '& return &' in result
        assert '\n' not in result

    def test_plain_text_unchanged(self):
        from notifier import _escape_applescript_literal
        assert _escape_applescript_literal("Hello World") == "Hello World"
