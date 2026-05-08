"""
crypto.py — 敏感字段加解密
==========================
对 users.json 中的密码、token 类字段做对称加密（Fernet）。
密钥存储在 .env 的 DATA_ENCRYPTION_KEY；首次运行时自动生成并写入。

格式：加密值以 "$F$" 开头，后跟 Fernet token（base64）。
解密时遇到不以 "$F$" 开头的值视为明文直通，保证向后兼容。
"""
import logging
import os
import threading

from cryptography.fernet import Fernet

from config import ENV_PATH

logger = logging.getLogger(__name__)

_ENV_KEY = "DATA_ENCRYPTION_KEY"
_CIPHER: Fernet | None = None
_CIPHER_LOCK = threading.Lock()


def _get_cipher() -> Fernet:
    global _CIPHER
    if _CIPHER is not None:
        return _CIPHER

    with _CIPHER_LOCK:
        # Double-checked locking：避免首次并发时生成多个不同密钥
        if _CIPHER is not None:
            return _CIPHER

        key = os.environ.get(_ENV_KEY, "").strip()
        if not key:
            from config import write_env_key
            key = Fernet.generate_key().decode()
            write_env_key(_ENV_KEY, key)
            os.environ[_ENV_KEY] = key
            logger.info("已生成数据加密密钥并写入 .env")

        _CIPHER = Fernet(key.encode())
        return _CIPHER


def encrypt(plaintext: str) -> str:
    """加密字符串。空字符串原样返回。"""
    if not plaintext:
        return ""
    cipher = _get_cipher()
    return "$F$" + cipher.encrypt(plaintext.encode()).decode()


def decrypt(maybe_encrypted: str) -> str:
    """
    解密字符串。
    - 无 "$F$" 前缀 → 明文，原样返回（向后兼容旧数据）
    - 有前缀 → Fernet 解密；密钥不匹配时抛异常并给出明确提示
    """
    if not maybe_encrypted or not maybe_encrypted.startswith("$F$"):
        return maybe_encrypted
    cipher = _get_cipher()
    try:
        return cipher.decrypt(maybe_encrypted[3:].encode()).decode()
    except Exception:
        logger.critical(
            "数据解密失败！DATA_ENCRYPTION_KEY 可能已被更换。"
            "请检查 .env 中该 key 是否与写入数据时一致。"
        )
        raise
