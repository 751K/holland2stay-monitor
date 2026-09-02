"""
crypto.py — 敏感字段加解密
==========================
对 SQLite user_configs / 旧 users.json 中的密码、token 类字段做对称加密（Fernet）。
密钥存储在 .env 的 DATA_ENCRYPTION_KEY；首次运行时自动生成并写入。

格式：加密值以 "$F$" 开头，后跟 Fernet token（base64）。
解密时遇到不以 "$F$" 开头的值视为明文直通，保证向后兼容。
"""
import logging
import os
import threading

from cryptography.fernet import Fernet

from config import ENV_PATH  # noqa: F401  — 兼容旧的 monkeypatch 目标

logger = logging.getLogger(__name__)

_ENV_KEY = "DATA_ENCRYPTION_KEY"
_CIPHER: Fernet | None = None
_CIPHER_LOCK = threading.Lock()


class MissingEncryptionKey(RuntimeError):
    """库里有密文，但 ``DATA_ENCRYPTION_KEY`` 不在环境里。

    这种情况**必须停下**：自动生成一把新钥匙不会报任何错，但那些密文从此再也
    解不开——而且是静默的，等到有人去读某个用户的密码才发现，那时新钥匙已经把
    更多数据加密了一遍，回滚也救不回来。
    """


def _env_path():
    """.env 的路径，**调用时**从 config 取。

    模块级 ``from config import ENV_PATH`` 是导入时绑定的副本，而写入端
    (``config.write_env_key``) 用的是 ``config.ENV_PATH``。两边指向不同变量时
    「读不到刚写进去的密钥」——正是这段代码最不该出错的地方。同一个来源取值，
    这种偏差就不可能发生。
    """
    import config
    return config.ENV_PATH


def _read_key_from_env_file() -> str:
    """直接从 .env 读密钥，绕过 ``os.environ`` 的进程启动快照。

    拿到跨进程锁之后必须重读文件：另一个进程可能在我们等锁的这段时间里生成并
    写好了密钥，而我们的 ``os.environ`` 是启动时的快照，永远看不到它。
    """
    try:
        env_path = _env_path()
        if not env_path.exists():
            return ""
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == _ENV_KEY:
                return v.strip().strip('"').strip("'")
    except OSError:
        logger.debug("读取 .env 失败，按「没有密钥」处理", exc_info=True)
    return ""


def _ciphertext_exists() -> bool:
    """库里是否已经有 ``$F$`` 密文。

    只用来回答「能不能安全地生成一把新钥匙」。读不出来时返回 **True**——
    fail-safe：拿不准就别生成，宁可让人手动确认，也不要把已有数据锁死。
    """
    import sqlite3

    try:
        from config import load_config
        db = load_config().db_path
    except Exception:
        db = os.environ.get("DB_PATH", "data/listings.db")
    try:
        if not os.path.exists(db):
            return False                     # 库都没有，必然是首次运行
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
            for t in tables:
                cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')]
                text_cols = [c for c in cols if c]
                if not text_cols:
                    continue
                where = " OR ".join(f'"{c}" LIKE \'$F$%\'' for c in text_cols)
                try:
                    if conn.execute(
                            f'SELECT 1 FROM "{t}" WHERE {where} LIMIT 1').fetchone():
                        return True
                except sqlite3.Error:
                    continue                 # 二进制列等，跳过
            return False
        finally:
            conn.close()
    except Exception:
        logger.warning("无法确认库里有没有密文，按「有」处理（不自动生成密钥）",
                       exc_info=True)
        return True


def _get_cipher() -> Fernet:
    global _CIPHER
    if _CIPHER is not None:
        return _CIPHER

    with _CIPHER_LOCK:
        # 进程内的双检锁。**它挡不住跨进程**——monitor 与 gunicorn 是
        # supervisord 起的两个独立进程，冷启动时都会走到这里。
        if _CIPHER is not None:
            return _CIPHER

        key = os.environ.get(_ENV_KEY, "").strip()
        if not key:
            key = _generate_or_wait_for_key()

        _CIPHER = Fernet(key.encode())
        return _CIPHER


def _generate_or_wait_for_key() -> str:
    """没有密钥时：跨进程互斥地生成一把，或读到别人刚生成的那把。

    为什么 ``threading.Lock`` 不够
    ------------------------------
    容器里 supervisord 同时起 ``monitor`` 和 ``gunicorn`` 两个**进程**。两边冷
    启动时都没有密钥，各自的线程锁互不可见，于是各生成一把、各自写 .env——后写的
    赢，先写的那个进程却继续用自己内存里那把加密。**用旧钥匙写进库的密文从此
    永远解不开**，而且全程不报错。

    （``app/env_writer.py`` 的注释写着「crypto.py 的写入是单点触发，不会与 web
    并发」——那个假设只在密钥已经存在时成立。）

    所以用文件锁：拿到锁之后**重读 .env**，另一个进程赢了的话我们直接用它的。
    """
    import fcntl

    lock_path = _env_path().parent / ".data-encryption-key.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        # 拿不到锁文件（只读挂载等）：退化成无锁路径，但仍然保留下面的
        # 「有密文就拒绝」那道闸——那道才是防数据丢失的关键。
        logger.warning("无法创建密钥锁文件，本次退化为无跨进程互斥", exc_info=True)
        return _generate_key_unlocked()

    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        # 等锁期间别人可能已经写好了。os.environ 是启动快照，看不到，必须读文件。
        key = _read_key_from_env_file()
        if key:
            os.environ[_ENV_KEY] = key
            logger.info("使用另一个进程刚生成的数据加密密钥")
            return key
        return _generate_key_unlocked()
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _generate_key_unlocked() -> str:
    """真正生成并落盘。**已有密文时拒绝**。"""
    if _ciphertext_exists():
        raise MissingEncryptionKey(
            f"{_ENV_KEY} 不在环境里，但数据库中已存在 $F$ 密文。\n"
            "自动生成新密钥会让这些密文永久无法解密，因此拒绝启动。\n"
            "请把原来的 DATA_ENCRYPTION_KEY 放回 .env；确实丢失了的话，"
            "需要先清掉受影响的加密字段，再让本进程生成新密钥。"
        )
    from config import write_env_key
    key = Fernet.generate_key().decode()
    write_env_key(_ENV_KEY, key)
    os.environ[_ENV_KEY] = key
    logger.info("已生成数据加密密钥并写入 .env")
    return key


def encrypt(plaintext: str) -> str:
    """加密字符串。空字符串原样返回。

    **幂等**：已经带 "$F$" 前缀的值原样返回，不会套第二层。

    没有这道判断的话，重复加密会产出 ``$F$ + encrypt("$F$…")``，而 ``decrypt``
    只解一层，返回的是内层密文本身——看起来像是"解密成功"，实际拿到一串乱码，
    静默损坏且很难倒查。迁移脚本和保存路径都可能对同一个值调两次，所以这道
    判断必须在这里，而不是靠每个调用方自己记得先检查。

    代价是无法加密一个真的以 "$F$" 开头的明文；但 ``decrypt`` 早就把该前缀当作
    标记了，这种明文本来就无法往返，不算新增限制。
    """
    if not plaintext:
        return ""
    if plaintext.startswith("$F$"):
        return plaintext
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
