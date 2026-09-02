"""数据加密密钥的引导：跨进程互斥，且不许在有密文时另生成一把。

两个独立的坑
------------
**一、``threading.Lock`` 挡不住跨进程。** 容器里 supervisord 同时起 ``monitor``
与 ``gunicorn`` 两个进程（gunicorn 本身是 ``--workers=1``，但它和 monitor 是两个
进程）。冷启动时两边都没有密钥，各自的线程锁互不可见，于是各生成一把、各写 .env
——后写的赢，先写的那个进程却继续用自己内存里那把加密。**用旧钥匙写进库的密文从此
永远解不开**，全程不报错。

**二、.env 丢了就静默换钥匙。** 原实现「没有密钥就生成一把」，不看库里有没有已经
加密过的数据。生产实测 ``user_configs.telegram_token`` 有 11 行 ``$F$`` 密文——
.env 一丢，这 11 行当场变成永远解不开的乱码，而且要等到有人去读某个用户的 token
才会发现，那时新钥匙已经把更多数据加密了一遍。
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import textwrap

import pytest

import crypto


@pytest.fixture
def clean_env(tmp_path, monkeypatch):
    """一个空的 .env 与空库，且进程内没有已缓存的 cipher。"""
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    # 读写两端都要指到同一个文件：crypto 读 config.ENV_PATH（_env_path 调用时取），
    # config.write_env_key 也写 config.ENV_PATH。
    import config
    monkeypatch.setattr(config, "ENV_PATH", env)
    monkeypatch.setattr(crypto, "ENV_PATH", env)
    monkeypatch.delenv("DATA_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(crypto, "_CIPHER", None)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "listings.db"))
    return env


def _make_db_with_ciphertext(path) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE user_configs (id TEXT, telegram_token TEXT)")
    conn.execute("INSERT INTO user_configs VALUES ('u1', '$F$gAAAAABsomething')")
    conn.commit()
    conn.close()


class TestRefusesWhenCiphertextExists:
    def test_raises_instead_of_generating(self, clean_env, tmp_path, monkeypatch):
        """库里有 $F$ 而 .env 没钥匙 → 停下，不要另生成一把。

        生成一把新的不会报任何错，但那些密文从此再也解不开。宁可起不来。
        """
        _make_db_with_ciphertext(tmp_path / "listings.db")
        monkeypatch.setattr(crypto, "_CIPHER", None)
        with pytest.raises(crypto.MissingEncryptionKey):
            crypto._get_cipher()
        assert "DATA_ENCRYPTION_KEY" not in clean_env.read_text(), (
            "拒绝之后不该留下一把新钥匙")

    def test_message_says_what_to_do(self, clean_env, tmp_path, monkeypatch):
        """报错要能直接照做——「解密失败」这种消息只会让人去重装。"""
        _make_db_with_ciphertext(tmp_path / "listings.db")
        monkeypatch.setattr(crypto, "_CIPHER", None)
        with pytest.raises(crypto.MissingEncryptionKey) as ei:
            crypto._get_cipher()
        msg = str(ei.value)
        assert "DATA_ENCRYPTION_KEY" in msg and ".env" in msg

    def test_unreadable_db_is_treated_as_having_ciphertext(self, clean_env, monkeypatch):
        """读不出库时按「有密文」处理——fail-safe。

        反过来（读不出就当没有）会在库暂时锁住 / 路径配错时静默换钥匙，
        那正是这个 bug 最危险的形态。
        """
        monkeypatch.setenv("DB_PATH", "/nonexistent-dir/x.db")

        def _boom(*_a, **_kw):
            raise sqlite3.OperationalError("locked")

        monkeypatch.setattr(sqlite3, "connect", _boom)
        monkeypatch.setattr(os.path, "exists", lambda _p: True)
        assert crypto._ciphertext_exists() is True


class TestGeneratesOnFirstRun:
    def test_empty_deployment_still_bootstraps(self, clean_env, monkeypatch):
        """没有库 = 首次运行，照常生成，不该把新部署也拦下来。"""
        monkeypatch.setattr(crypto, "_CIPHER", None)
        c = crypto._get_cipher()
        assert c is not None
        assert "DATA_ENCRYPTION_KEY" in clean_env.read_text()

    def test_roundtrip_after_bootstrap(self, clean_env, monkeypatch):
        monkeypatch.setattr(crypto, "_CIPHER", None)
        assert crypto.decrypt(crypto.encrypt("hunter2")) == "hunter2"


class TestCrossProcess:
    def test_env_file_is_reread_after_taking_the_lock(self, clean_env, monkeypatch):
        """拿到锁之后必须**重读 .env**，不能信 os.environ 的启动快照。

        另一个进程可能在我们等锁时写好了密钥；读快照的话我们会以为还没有，
        于是再生成一把——这正是两把钥匙的由来。
        """
        monkeypatch.setattr(crypto, "_CIPHER", None)
        # 模拟「等锁期间别人写好了」：文件里有，os.environ 里没有
        clean_env.write_text("DATA_ENCRYPTION_KEY=" + _valid_key() + "\n",
                             encoding="utf-8")
        assert os.environ.get("DATA_ENCRYPTION_KEY") is None

        key = crypto._generate_or_wait_for_key()
        assert key == _valid_key(), "没有重读 .env，又生成了一把新的"

    def test_two_processes_end_up_with_the_same_key(self, tmp_path):
        """真起两个进程抢一次。线程锁在这里完全不起作用。

        用子进程而不是线程：threading.Lock 会让线程版永远通过，掩盖问题——
        这正是原实现「看起来有锁」的原因。
        """
        env = tmp_path / ".env"
        env.write_text("", encoding="utf-8")
        go = tmp_path / "GO"
        # ⚠️ DATA_ENCRYPTION_KEY 必须在 `import config` **之后**再清：config 会
        # load_dotenv()，把仓库根目录那个真实 .env 里的密钥灌进 os.environ。
        # 先清的话两个子进程都会读到同一把真钥匙，测试全绿而竞争根本没发生
        # ——这条用例第一版就是那样。
        script = textwrap.dedent(f"""
            import os, sys, time, pathlib
            sys.path.insert(0, {os.getcwd()!r})
            import config
            config.ENV_PATH = pathlib.Path({str(env)!r})
            os.environ.pop("DATA_ENCRYPTION_KEY", None)
            os.environ["DB_PATH"] = {str(tmp_path / "listings.db")!r}
            import crypto
            crypto.ENV_PATH = config.ENV_PATH
            crypto._CIPHER = None
            # 起跑栅栏：两个进程都就位后才开跑，尽量让它们真的撞上
            go = pathlib.Path({str(go)!r})
            for _ in range(2000):
                if go.exists():
                    break
                time.sleep(0.002)
            print(crypto._get_cipher()._signing_key.hex()[:16])
        """)
        outs = []
        procs = [subprocess.Popen([sys.executable, "-c", script],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True) for _ in range(2)]
        import time as _t
        _t.sleep(1.0)          # 等两个进程都到栅栏
        go.write_text("go")
        for p in procs:
            out, err = p.communicate(timeout=60)
            assert p.returncode == 0, err
            outs.append(out.strip())
        assert all(outs), f"子进程没输出密钥：{outs}"

        assert outs[0] == outs[1], (
            f"两个进程拿到了不同的密钥：{outs}——先写的那个的密文将永远解不开")
        assert env.read_text().count("DATA_ENCRYPTION_KEY") == 1, (
            ".env 里出现了多把钥匙")


def _valid_key() -> str:
    from cryptography.fernet import Fernet
    # 固定一把，避免每次调用生成不同的
    global _KEY
    try:
        return _KEY
    except NameError:
        pass
    _KEY = Fernet.generate_key().decode()
    return _KEY
