"""平台凭据不能互相回退。

拆分 H2S / Xior / OurDomain 三套凭据时，给后两个留了「缺失就回退到旧共用值」
的兜底。那个"旧共用值"实际上就是**用户的 H2S 账号密码**（当时只有 H2S 支持
自动预订），所以回填不是"保住了旧配置"，而是凭空替用户编了两个他从来没注册
过的账号。

2026-08-04 实测：全部用户的 ``ourdomain_email`` / ``xior_email`` 与其 H2S 邮箱
**逐字相同**，而且已经固化进 ``auto_book_json``。开了自动预订的用户里没有一个
真的注册过 OurDomain 或 Xior 账号。

后果不是"登录失败"这么轻：

1. 把用户的 H2S 密码发给第三方站点；
2. RENTCafe 按 **IP** 记登录失败（连续失败锁 30 分钟），同一代理池上的其他
   用户跟着遭殃；
3. 它把 Xior「**没配该楼凭据 = 该楼不参与**」的设计短路了——凭据本身就是
   开关，凭空造一份等于把开关焊死在"开"上。

两个平台当时都不在 ``_AUTO_BOOK_SOURCES`` 里，所以没爆。
"""
from __future__ import annotations

import json

import pytest

import users as users_mod
from config import AutoBookConfig
from crypto import encrypt
from mstorage import Storage
from users import (
    RENTCAFE_CRED_CLEANUP_META_KEY,
    UserConfig,
    _ab_from_dict,
    _ensure_rentcafe_creds_unbackfilled,
)

H2S_EMAIL = "me@h2s.example"
H2S_PW = "h2s-secret"


# ── 加载层：以后不会再回填 ────────────────────────────────────────────


class TestNoFallbackOnLoad:
    def test_missing_platform_creds_stay_empty(self):
        """旧数据（只有共用 email/password）不该长出另外两个平台的账号。"""
        ab = _ab_from_dict({"email": H2S_EMAIL, "password": encrypt(H2S_PW)})
        assert ab.email == H2S_EMAIL and ab.password == H2S_PW
        assert ab.ourdomain_email == "" and ab.ourdomain_password == ""
        assert ab.xior_email == "" and ab.xior_password == ""

    def test_credentials_are_the_switch(self):
        """没配 = 不参与。回填会把这个开关焊死在"开"上。"""
        ab = _ab_from_dict({"email": H2S_EMAIL, "password": encrypt(H2S_PW)})
        assert ab.xior_account_for("p0196062") == ("", "")

    def test_real_platform_creds_are_kept(self):
        ab = _ab_from_dict({
            "email": H2S_EMAIL, "password": encrypt(H2S_PW),
            "ourdomain_email": "me@od.example",
            "ourdomain_password": encrypt("od-secret"),
        })
        assert ab.ourdomain_email == "me@od.example"
        assert ab.ourdomain_password == "od-secret"

    def test_h2s_creds_still_load(self):
        """只动另外两个平台，H2S 自己那对不受影响。"""
        ab = _ab_from_dict({"email": H2S_EMAIL, "password": encrypt(H2S_PW)})
        assert (ab.email, ab.password) == (H2S_EMAIL, H2S_PW)


# ── 存量数据：库里那份也要洗 ──────────────────────────────────────────


@pytest.fixture
def store(tmp_path):
    s = Storage(tmp_path / "t.db")
    yield s
    s.close()


NOW = "2026-08-04T12:00:00+00:00"


def _seed(st, uid: str, ab, *, raw: str = "") -> None:
    st.conn.execute(
        "INSERT INTO user_configs (id, name, auto_book_json, created_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (uid, uid, raw or json.dumps(ab), NOW, NOW),
    )
    st.conn.commit()


def _read(st, uid: str) -> dict:
    row = st.conn.execute(
        "SELECT auto_book_json FROM user_configs WHERE id=?", (uid,)
    ).fetchone()
    return json.loads(row[0])


def _backfilled(**over) -> dict:
    """线上那 41 行长的样子：三个平台的凭据一模一样。"""
    ab = {
        "email": H2S_EMAIL, "password": encrypt(H2S_PW),
        "xior_email": H2S_EMAIL, "xior_password": encrypt(H2S_PW),
        "ourdomain_email": H2S_EMAIL, "ourdomain_password": encrypt(H2S_PW),
    }
    ab.update(over)
    return ab


class TestStoredCleanup:
    """光改加载层不够——回填过的值已经固化进 ``auto_book_json`` 了。"""

    def test_clears_both_platforms(self, store):
        _seed(store, "u1", _backfilled())
        _ensure_rentcafe_creds_unbackfilled(store)
        ab = _read(store, "u1")
        assert ab["ourdomain_email"] == "" and ab["ourdomain_password"] == ""
        assert ab["xior_email"] == "" and ab["xior_password"] == ""

    def test_h2s_pair_is_never_touched(self, store):
        """清理的是被回填的那两个平台，不是用户真配了的 H2S。"""
        _seed(store, "u1", _backfilled())
        _ensure_rentcafe_creds_unbackfilled(store)
        ab = _read(store, "u1")
        assert ab["email"] == H2S_EMAIL
        assert ab["password"], "H2S 密码被误删了"

    def test_genuinely_configured_platform_creds_survive(self, store):
        _seed(store, "u1", _backfilled(
            ourdomain_email="me@od.example",
            ourdomain_password=encrypt("od-secret"),
        ))
        _ensure_rentcafe_creds_unbackfilled(store)
        ab = _read(store, "u1")
        assert ab["ourdomain_email"] == "me@od.example"
        assert ab["xior_email"] == "", "Xior 那对仍是回填的，该清"

    def test_same_email_different_password_is_left_alone(self, store):
        """只对上一半不算回填指纹——多半是用户两边用了同一个邮箱。"""
        _seed(store, "u1", _backfilled(ourdomain_password=encrypt("different")))
        _ensure_rentcafe_creds_unbackfilled(store)
        assert _read(store, "u1")["ourdomain_email"] == H2S_EMAIL

    def test_encrypted_passwords_are_compared_as_plaintext(self, store):
        """Fernet 不是确定性加密：同一明文两次密文不同，比密文永远不相等。

        直接比密文的话这条清理会一条都清不掉，而且完全不报错。
        """
        ab = _backfilled()
        assert ab["password"] != ab["ourdomain_password"], "前提：密文本就不同"
        _seed(store, "u1", ab)
        _ensure_rentcafe_creds_unbackfilled(store)
        assert _read(store, "u1")["ourdomain_password"] == ""

    def test_user_without_h2s_creds_is_skipped(self, store):
        """没配过 H2S 就谈不上被回填，别去动人家的数据。"""
        ab = {"email": "", "password": "",
              "ourdomain_email": "me@od.example",
              "ourdomain_password": encrypt("od-secret")}
        _seed(store, "u1", ab)
        _ensure_rentcafe_creds_unbackfilled(store)
        assert _read(store, "u1")["ourdomain_email"] == "me@od.example"

    def test_runs_once_and_stays_out_of_the_way(self, store):
        """清完之后用户填什么就是什么——包括故意两边同密码。"""
        _seed(store, "u1", _backfilled())
        _ensure_rentcafe_creds_unbackfilled(store)
        assert store.get_meta(RENTCAFE_CRED_CLEANUP_META_KEY, default="") == "1"

        # 用户重新填了，而且真的用了和 H2S 一样的邮箱密码
        store.conn.execute(
            "UPDATE user_configs SET auto_book_json=? WHERE id=?",
            (json.dumps(_backfilled()), "u1"),
        )
        store.conn.commit()
        _ensure_rentcafe_creds_unbackfilled(store)      # 第二次是 no-op
        assert _read(store, "u1")["ourdomain_email"] == H2S_EMAIL

    def test_broken_json_does_not_abort_the_whole_cleanup(self, store):
        """一行坏数据不该让其他用户的清理全部落空。"""
        _seed(store, "bad", None, raw="{not json")
        _seed(store, "u1", _backfilled())
        _ensure_rentcafe_creds_unbackfilled(store)
        assert _read(store, "u1")["ourdomain_email"] == ""

    def test_no_users_still_sets_the_flag(self, store):
        _ensure_rentcafe_creds_unbackfilled(store)
        assert store.get_meta(RENTCAFE_CRED_CLEANUP_META_KEY, default="") == "1"


class TestEndToEnd:
    def test_load_users_returns_no_phantom_platform_accounts(self, store, monkeypatch):
        """走完整 load_users：加载层 + 存量清理一起生效。"""
        _seed(store, "u1", _backfilled())
        store.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (users_mod.USERS_MIGRATION_META_KEY, "1"),
        )
        store.conn.commit()
        monkeypatch.setattr(users_mod, "_open_storage", lambda: store)
        monkeypatch.setattr(store, "close", lambda: None)

        (u,) = users_mod.load_users()
        assert u.auto_book.ourdomain_email == ""
        assert u.auto_book.xior_account_for("p0196062") == ("", "")
        assert u.auto_book.email == H2S_EMAIL, "H2S 那对该留着"
