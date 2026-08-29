"""
验证邮件此前整封写死中文，落款还挂着「Holland2Stay 房源监控」。

两处都不只是文案问题：

* ``UserConfig.language`` 默认就是 ``en``，面板上也没有任何地方能改它。
  2026-08-29 线上 62 个用户全部是 en，配了邮箱的 13 个也全部是 en——至今发出去
  的每一封验证邮件都是中文发给英文用户的。房源通知一直按这个字段分支，只有这
  一封没跟上。

* 项目监控四个平台，Holland2Stay 只是其中之一。通知邮件的落款早就改成了
  ``independent rental listing companion``，验证邮件停在旧文案上。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.email_verify import _VERIFY_FOOTER, _VERIFY_TEXT, _format_verify_email

_SRC = Path(__file__).resolve().parent.parent / "app" / "email_verify.py"


def _mk(lang="en", name="Kong"):
    return _format_verify_email("https://flatradar.app/verify/tok", name, lang)


# ── 语言 ────────────────────────────────────────────────────────

class TestLanguage:
    def test_english_by_default(self):
        """默认必须是英文——UserConfig.language 的默认值就是 en。"""
        subject, text, html = _mk("en")
        assert "Confirm your notification email" in subject
        assert "Hi Kong" in text
        assert '<html lang="en">' in html

    def test_chinese(self):
        subject, text, html = _mk("zh")
        assert "确认你的通知邮箱" in subject
        assert "你好 Kong" in text
        assert '<html lang="zh">' in html

    @pytest.mark.parametrize("lang", ["", None, "fr", "de-DE", "ZH_CN", "en-US"])
    def test_unknown_language_falls_back_to_english(self, lang):
        """认不出的值走英文，而不是抛异常或退回中文。

        ``zh_CN`` / ``en-US`` 这种带地区的写法要认得出主语言。
        """
        subject, _, html = _format_verify_email("https://x/y", "Kong", lang)
        want_zh = (lang or "").lower().startswith("zh")
        assert ("确认你的通知邮箱" in subject) is want_zh, lang
        assert f'<html lang="{"zh" if want_zh else "en"}">' in html

    def test_html_lang_matches_the_body(self):
        for lang in ("zh", "en"):
            _, _, html = _mk(lang)
            assert f'<html lang="{lang}">' in html

    def test_both_languages_have_every_key(self):
        """少一个键会在渲染时 KeyError，而那是在发信路径上。"""
        assert set(_VERIFY_TEXT["zh"]) == set(_VERIFY_TEXT["en"])
        for lang, table in _VERIFY_TEXT.items():
            for k, v in table.items():
                assert v.strip(), f"{lang}.{k} 是空的"

    def test_no_chinese_left_in_the_english_version(self):
        """英文版正文里不许残留中文——漏改一句比整封没改更难发现。"""
        _, text, html = _mk("en")
        body = re.sub(r"<[^>]+>", " ", html)
        for blob in (text, body):
            assert not re.search(r"[一-鿿]", blob), \
                re.findall(r"[一-鿿]+", blob)


# ── 落款 ────────────────────────────────────────────────────────

class TestFooter:
    def test_no_platform_name_in_the_footer(self):
        """落款不该点名四个平台里的一个。"""
        for lang in ("zh", "en"):
            _, _, html = _mk(lang)
            assert "Holland2Stay" not in html
        # 注释里说得着旧文案，看的是真正会渲染出去的部分
        src = re.sub(r"#[^\n]*", "", _SRC.read_text(encoding="utf-8"))
        assert "Holland2Stay" not in src

    def test_footer_matches_the_notification_emails(self):
        """两种邮件同一句落款。分开写迟早只改一处。"""
        import notifier

        assert _VERIFY_FOOTER in notifier._format_email_html("[H2S] New Listing")

    def test_footer_is_language_independent(self):
        for lang in ("zh", "en"):
            _, _, html = _mk(lang)
            assert _VERIFY_FOOTER in html


# ── 用户名仍然被清洗 ────────────────────────────────────────────

class TestNameIsStillSanitised:
    def test_html_is_escaped(self):
        _, _, html = _mk("en", '<script>alert(1)</script>')
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_control_chars_do_not_forge_paragraphs(self):
        _, text, _ = _mk("en", "Kong\n\nURGENT: send money")
        assert "Kong URGENT: send money" in text.splitlines()[0]

    def test_name_is_escaped_exactly_once(self):
        """文案是本文件自己的常量，不含元字符；名字已在 safe_name 处转义过。

        外面再包一层 _html_escape 会把 ``&`` 变成 ``&amp;amp;``——名字里带 ``&``
        的人会在邮件里看到实体码，而这类名字不罕见（"Tom & Jerry"）。
        """
        _, _, html = _mk("en", "Tom & Jerry")
        assert "Tom &amp; Jerry" in html
        assert "&amp;amp;" not in html

    def test_static_strings_carry_no_html(self):
        """这条撑着上一条：文案不转义的前提是它本来就不含元字符。"""
        for lang, table in _VERIFY_TEXT.items():
            for k, v in table.items():
                assert not set(v) & set("<>&"), f"{lang}.{k} 里有 HTML 元字符: {v!r}"

    def test_name_reaches_both_languages(self):
        for lang in ("zh", "en"):
            _, text, html = _mk(lang, "Zhang")
            assert "Zhang" in text and "Zhang" in html


# ── 语言真的来自用户配置 ────────────────────────────────────────

def test_send_path_passes_the_user_language():
    """判据是 ``user.language``，和房源通知同一个字段。

    这条盯的是「路由确实把它传下去了」——签名带默认值 ``en``，忘记传不会报错，
    只会让所有人重新退回英文而无人察觉。
    """
    import inspect

    import app.routes.email_verify as rev
    import app.routes.users as ru

    for mod in (rev, ru):
        src = inspect.getsource(mod)
        i = src.index("send_verification_email_sync(\n")
        call = src[i:i + 260]
        assert "language" in call, f"{mod.__name__} 没有把用户语言传下去"


def test_signature_threads_lang_all_the_way():
    from app.email_verify import send_verification_email, send_verification_email_sync
    import inspect

    for fn in (send_verification_email, send_verification_email_sync):
        assert "lang" in inspect.signature(fn).parameters, fn.__name__
