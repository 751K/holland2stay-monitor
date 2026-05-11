"""
crypto.encrypt/decrypt 测试。

核心契约：
1. roundtrip：encrypt(plaintext) → decrypt(ciphertext) == plaintext
2. 旧明文兼容：没有 $F$ 前缀的字符串 decrypt 直接返回原文
3. 密钥不匹配：decrypt 抛异常（不能静默返回空字符串覆盖加密数据）
4. 空字符串：双向都返回 ""（不应该消耗密钥的随机性 quota）

为什么重要：
users.json 里存的 email_password / twilio_token / auto_book.password
全部走 crypto。如果 decrypt 出错被吞，启动时会被错误地用空字符串覆盖
真实数据，相当于"密钥变了一次就丢失所有凭据"。
"""
from __future__ import annotations

import pytest

from cryptography.fernet import Fernet


class TestEncryptDecryptRoundtrip:
    """正向：加密 → 解密 == 原文。"""

    def test_simple_ascii(self, fresh_crypto):
        from crypto import encrypt, decrypt
        assert decrypt(encrypt("hello")) == "hello"

    def test_unicode_chinese(self, fresh_crypto):
        from crypto import encrypt, decrypt
        assert decrypt(encrypt("你好，世界")) == "你好，世界"

    def test_special_chars(self, fresh_crypto):
        from crypto import encrypt, decrypt
        payload = '"quoted" \\backslash \n\t newline'
        assert decrypt(encrypt(payload)) == payload

    def test_long_password(self, fresh_crypto):
        from crypto import encrypt, decrypt
        # 模拟一个长 Twilio token / API key
        payload = "a" * 256
        assert decrypt(encrypt(payload)) == payload

    def test_ciphertext_has_fernet_prefix(self, fresh_crypto):
        """加密结果必须以 $F$ 开头，供 decrypt 区分加密/明文。"""
        from crypto import encrypt
        ct = encrypt("anything")
        assert ct.startswith("$F$"), f"missing $F$ prefix: {ct!r}"

    def test_ciphertext_differs_per_call(self, fresh_crypto):
        """Fernet 每次加密含随机 IV，同一明文两次加密不应相同。"""
        from crypto import encrypt
        # 但两次都能 decrypt 回原文
        ct1 = encrypt("same")
        ct2 = encrypt("same")
        assert ct1 != ct2, "Fernet 加密应该是非确定性的（含随机 IV）"


class TestEmptyString:
    """空字符串走快速路径，不走 Fernet。"""

    def test_encrypt_empty_returns_empty(self, fresh_crypto):
        from crypto import encrypt
        assert encrypt("") == ""

    def test_decrypt_empty_returns_empty(self, fresh_crypto):
        from crypto import decrypt
        assert decrypt("") == ""


class TestPlaintextCompatibility:
    """
    旧版数据兼容：users.json 升级前的字段没有 $F$ 前缀，按明文直通。
    这是迁移期间的关键路径 —— 不做这件事会导致旧数据全部 decrypt 失败。
    """

    def test_no_prefix_returns_as_is(self, fresh_crypto):
        from crypto import decrypt
        assert decrypt("legacy_plain_password") == "legacy_plain_password"

    def test_no_prefix_with_special_chars(self, fresh_crypto):
        from crypto import decrypt
        assert decrypt('legacy "quoted" \\value') == 'legacy "quoted" \\value'

    def test_prefix_in_middle_not_treated_as_encrypted(self, fresh_crypto):
        """$F$ 必须在最开头，中间出现不算加密。"""
        from crypto import decrypt
        s = "prefix$F$middle"
        # 不以 $F$ 开头 → 视为明文 → 原样返回
        assert decrypt(s) == s


class TestKeyMismatch:
    """
    密钥变化后 decrypt 必须**抛异常**，不能静默成功或返回空。
    场景：管理员误把 .env 替换成另一份 → 启动时所有用户的旧密码 decrypt 失败。
    如果静默返回 ""，下次 save_users() 会把空字符串重新加密保存，
    相当于丢失全部凭据。
    """

    def test_decrypt_with_wrong_key_raises(self, fresh_crypto):
        from crypto import encrypt, decrypt
        import crypto

        # 用第一个密钥加密
        ciphertext = encrypt("secret")
        assert ciphertext.startswith("$F$")

        # 模拟密钥变更：重置 _CIPHER 并塞入新密钥
        crypto._CIPHER = Fernet(Fernet.generate_key())

        # decrypt 必须抛异常（cryptography.fernet.InvalidToken 或类似）
        with pytest.raises(Exception):
            decrypt(ciphertext)

    def test_corrupted_ciphertext_raises(self, fresh_crypto):
        """密文被改坏（剪短 / 篡改）也必须抛。"""
        from crypto import encrypt, decrypt
        ct = encrypt("secret")
        # 改掉一个字符
        bad = ct[:-1] + ("A" if ct[-1] != "A" else "B")
        with pytest.raises(Exception):
            decrypt(bad)


class TestKeyAutoGenerate:
    """
    首次启动：DATA_ENCRYPTION_KEY 不在 .env 中，crypto 自动生成并写入。
    """

    def test_first_call_generates_and_persists_key(self, fresh_crypto, tmp_path):
        import crypto
        # fresh_crypto 已把 ENV_PATH 指向 tmp_path/.env 且清空 env var
        assert crypto._CIPHER is None

        # 第一次 encrypt 触发密钥生成
        ct = crypto.encrypt("trigger")
        assert ct.startswith("$F$")
        assert crypto._CIPHER is not None  # 已缓存

        # .env 文件应该有 DATA_ENCRYPTION_KEY 一行
        env_content = (tmp_path / ".env").read_text(encoding="utf-8")
        assert "DATA_ENCRYPTION_KEY=" in env_content
