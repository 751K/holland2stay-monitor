"""
前端辅助函数 + XSS 回归测试。

覆盖：
- _mask_email() 脱敏
- templates 中 XSS 风险点验证（无 innerHTML 拼接用户输入）
"""
from __future__ import annotations

import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape


# ── _mask_email ────────────────────────────────────────────

class TestMaskEmail:
    def test_normal_email(self):
        from bookers.h2s_checkout import _mask_email
        assert _mask_email("test@example.com") == "tes***@example.com"

    def test_short_local(self):
        from bookers.h2s_checkout import _mask_email
        assert _mask_email("ab@x.com") == "***@x.com"

    def test_empty(self):
        from bookers.h2s_checkout import _mask_email
        assert _mask_email("") == "***"

    def test_no_at_sign(self):
        from bookers.h2s_checkout import _mask_email
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
        """测试通知结果用 innerHTML 布局，动态错误字段必须先 escape。

        账号页（account.js）和用户管理页（users.js）各有一份结果渲染。
        """
        static = Path(__file__).parent.parent / "static"
        for name in ("account.js", "users.js"):
            source = static.joinpath(name).read_text(encoding="utf-8")
            for field in ("data.error", "r.channel", "r.error"):
                assert f"${{{field}}}" not in source, f"{name} 直接插入了 {field}"
                assert f"escape({field})" in source, f"{name} 没有转义 {field}"

    def test_hidden_email_to_is_not_statically_required(self):
        """Email 通道未启用时隐藏的收件邮箱字段不能阻断表单提交。"""
        source = (Path(__file__).parent.parent / "templates" / "user_form.html").read_text(
            encoding="utf-8",
        )
        match = re.search(r'<input[^>]+id="email_to_input"[^>]*>', source)
        assert match is not None
        assert " required" not in match.group(0)

    def test_telegram_setup_hint_present(self):
        """Telegram 通知配置应提示用户如何获取 Bot Token 和 Chat ID。"""
        source = (Path(__file__).parent.parent / "templates" / "user_form.html").read_text(
            encoding="utf-8",
        )
        assert "@BotFather" in source
        assert "/newbot" in source
        assert "getUpdates" in source
        assert "Chat ID" in source

    def test_imessage_macos_only_hint_present(self):
        """iMessage 通知配置应提示只能在本地 macOS 环境使用。"""
        source = (Path(__file__).parent.parent / "templates" / "user_form.html").read_text(
            encoding="utf-8",
        )
        assert "本地 macOS" in source
        assert "Docker" in source
        assert "servers, Linux, or Docker" in source
