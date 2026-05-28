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

    def test_hidden_email_to_is_not_statically_required(self):
        """Email 通道未启用时隐藏的收件邮箱字段不能阻断表单提交。"""
        source = (Path(__file__).parent.parent / "templates" / "user_form.html").read_text(
            encoding="utf-8",
        )
        match = re.search(r'<input[^>]+id="email_to_input"[^>]*>', source)
        assert match is not None
        assert " required" not in match.group(0)
        assert "syncEmailRequirement()" in source

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

    def test_stats_range_updates_kpi_cards(self):
        """统计页切换天数时 KPI 卡片通过 JS setText 动态更新。"""
        source = (Path(__file__).parent.parent / "templates" / "stats.html").read_text(
            encoding="utf-8",
        )
        # v1.7.x KPI 元素由 JS renderSummary() 动态注入，不写静态 HTML id
        assert "renderSummary(d.summary)" in source
        assert "summary.new_range" in source
        assert "summary.changes_range" in source
        assert "{ cache: 'no-store' }" in source

    def test_listings_page_shows_last_seen_for_stale_sweep_debugging(self):
        """列表页应显示 stale 收敛真正使用的 last_seen，而不只显示 first_seen。"""
        source = (Path(__file__).parent.parent / "templates" / "listings.html").read_text(
            encoding="utf-8",
        )
        assert "{{ _('col_last_seen') }}" in source
        assert "l.last_seen | time_ago" in source
