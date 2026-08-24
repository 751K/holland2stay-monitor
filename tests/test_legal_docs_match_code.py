"""法务文档里的事实性声明必须和代码对得上。

这四份文档是**对外承诺**，写错的代价和代码 bug 不同：用户据此决定要不要用，
监管据此判断合规。而它们最容易的失效方式不是写错，是**代码变了而文档没跟着变**
——2026-08-24 的审查就是这么发现三处的：

    申请人档案（含证件号、国籍、犯罪记录）整类没在「我们收集什么」里出现
    使用条款说凭据「尽可能存在你设备本地」，实际服务端存着 Telegram token 等
    实际用到的 Resend / Photon 没点名

这里只钉**能从代码验证的事实**，不碰措辞。
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

_LEGAL = Path(__file__).resolve().parent.parent / "app" / "legal"
_EN = {"privacy": "privacy.txt", "terms": "terms.txt"}
_ZH = {"privacy": "privacyzh.txt", "terms": "termszh.txt"}


def _txt(name: str) -> str:
    return (_LEGAL / name).read_text(encoding="utf-8")


class TestBilingualParity:
    """中英必须同时改。只改一边是这类文档最常见的失效方式。"""

    @pytest.mark.parametrize("kind", ["privacy", "terms"])
    def test_same_number_of_sections(self, kind):
        import re
        en = len(re.findall(r"^\d+\. ", _txt(_EN[kind]), re.M))
        zh = len(re.findall(r"^\d+\. ", _txt(_ZH[kind]), re.M))
        assert en == zh, f"{kind}: 英文 {en} 节，中文 {zh} 节"

    @pytest.mark.parametrize("kind", ["privacy", "terms"])
    def test_same_last_updated_date(self, kind):
        import re
        en = re.search(r"Last updated: (\w+) (\d+), (\d{4})", _txt(_EN[kind]))
        zh = re.search(r"最后更新日期：(\d{4})年(\d+)月(\d+)日", _txt(_ZH[kind]))
        assert en and zh, f"{kind}: 找不到更新日期"
        months = ["January", "February", "March", "April", "May", "June", "July",
                  "August", "September", "October", "November", "December"]
        assert (int(zh.group(1)), int(zh.group(2)), int(zh.group(3))) == \
               (int(en.group(3)), months.index(en.group(1)) + 1, int(en.group(2))), \
               f"{kind}: 中英更新日期不一致"


class TestApplicantDataIsDisclosed:
    """服务端会存证件号、国籍、犯罪记录——隐私政策必须说。"""

    @pytest.mark.parametrize("name", ["privacy.txt", "privacyzh.txt"])
    def test_auto_booking_category_exists(self, name):
        t = _txt(name).lower()
        assert "auto-booking" in t or "自动预订" in _txt(name)

    @pytest.mark.parametrize("name,words", [
        ("privacy.txt", ["identity document number", "nationality", "convicted"]),
        ("privacyzh.txt", ["证件号码", "国籍", "定罪"]),
    ])
    def test_the_sensitive_fields_are_named(self, name, words):
        t = _txt(name)
        for w in words:
            assert w.lower() in t.lower(), f"{name} 没提到「{w}」"

    def test_criminal_data_is_reconciled_with_the_no_sensitive_claim(self):
        """§3 声称「不主动收集敏感数据」，必须同时说清这一处例外。"""
        en = _txt("privacy.txt")
        i = en.index("does not intentionally collect sensitive personal data")
        j = en.index("does not sell your personal data")
        assert "exception" in en[i:j].lower(), \
            "§3 没有说明自动预订这处例外，和 §2 自相矛盾"

    @pytest.mark.parametrize("name", ["privacy.txt", "privacyzh.txt"])
    def test_says_it_is_opt_in(self, name):
        t = _txt(name)
        assert ("disabled by default" in t) or ("默认关闭" in t), \
            "必须说明自动预订默认关闭，否则读者会以为人人都被收集了"


class TestEncryptionClaimsAreTrue:
    """文档说「加密存储」，代码就得真的加密。"""

    @pytest.mark.parametrize("name", ["privacy.txt", "privacyzh.txt"])
    def test_claims_encryption_at_rest(self, name):
        t = _txt(name)
        assert ("encrypted at rest" in t) or ("加密存储" in t)

    def test_the_claim_matches_the_code(self):
        from config import ApplicantProfile, profile_field_is_encrypted
        for f in dataclasses.fields(ApplicantProfile):
            if f.name in ("no_middle_name", "min_lease_term", "housing_type"):
                continue
            assert profile_field_is_encrypted(f.name), (
                f"文档承诺申请人资料加密存储，但 {f.name} 没加密"
            )

    def test_third_party_credentials_are_encrypted(self):
        """文档说自配通知渠道的凭据加密存储。"""
        import users
        src = Path(users.__file__).read_text(encoding="utf-8")
        for field in ("email_password", "telegram_token", "twilio_token"):
            assert field in src and "encrypt(" in src


class TestThirdPartiesAreNamed:
    """实际会收到数据的第三方要点名，不能只写「邮件或支持服务提供商」。"""

    @pytest.mark.parametrize("name,vendors", [
        ("privacy.txt", ["Resend", "Komoot", "Hetzner"]),
        ("privacyzh.txt", ["Resend", "Komoot", "Hetzner"]),
    ])
    def test_named(self, name, vendors):
        t = _txt(name)
        for v in vendors:
            assert v in t, f"{name} 没点名 {v}"

    def test_resend_is_actually_used(self):
        """反向核对：文档点名的服务确实在代码里。"""
        root = Path(__file__).resolve().parent.parent
        assert "api.resend.com" in (root / "notifier.py").read_text(encoding="utf-8")

    def test_photon_is_actually_used(self):
        root = Path(__file__).resolve().parent.parent
        src = (root / "app" / "routes" / "map_routes.py").read_text(encoding="utf-8")
        assert "photon.komoot.io" in src


class TestTermsCredentialClause:
    """条款曾说凭据「尽可能存在你设备本地」，而服务端确实存着一批。"""

    @pytest.mark.parametrize("name", ["terms.txt", "termszh.txt"])
    def test_admits_server_side_storage(self, name):
        t = _txt(name)
        assert ("stored on FlatRadar servers in encrypted form" in t) or \
               ("以加密形式存储在 FlatRadar 服务器上" in t), \
               f"{name} 仍在暗示凭据只存在设备本地"

    @pytest.mark.parametrize("name", ["terms.txt", "termszh.txt"])
    def test_tells_users_how_to_remove_them(self, name):
        t = _txt(name)
        assert ("remove any stored third-party credential" in t) or \
               ("清除任何已存储的第三方凭据" in t)
