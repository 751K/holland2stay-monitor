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
from crypto import encrypt
from mstorage import Storage
from users import (
    RENTCAFE_CRED_CLEANUP_META_KEY,
    USERS_MIGRATION_META_KEY,
    _ab_from_dict,
    check_legacy_migrations,
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
# ── 存量数据：旧版一次性清理已删除，只剩启动检查 ──────────────────────
#
# 库里那份回填值由 v2.1 及以前的一次性清理洗掉。清理代码删了之后，没洗过的旧库
# 必须被挡在门外，而不是悄悄带着别人的 H2S 密码跑起来。


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(users_mod, "USERS_FILE", tmp_path / "users.json")
    s = Storage(tmp_path / "t.db")
    yield s
    s.close()


NOW = "2026-08-04T12:00:00+00:00"


def _seed(st, uid: str) -> None:
    st.conn.execute(
        "INSERT INTO user_configs (id, name, auto_book_json, created_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (uid, uid, json.dumps({"email": H2S_EMAIL, "password": encrypt(H2S_PW)}), NOW, NOW),
    )
    st.conn.commit()


def _flags(st) -> list[str]:
    return [st.get_meta(k, default="")
            for k in (USERS_MIGRATION_META_KEY, RENTCAFE_CRED_CLEANUP_META_KEY)]


class TestLegacyMigrationCheck:
    def test_fresh_install_gets_both_flags(self, store):
        check_legacy_migrations(store)
        assert _flags(store) == ["1", "1"]

    def test_migrated_database_passes(self, store):
        _seed(store, "u1")
        for k in (USERS_MIGRATION_META_KEY, RENTCAFE_CRED_CLEANUP_META_KEY):
            store.set_meta(k, "1")
        users_mod.USERS_FILE.write_text("[]")   # 迁移后 users.json 原样留着
        check_legacy_migrations(store)

    def test_never_imported_users_json_is_refused(self, store):
        users_mod.USERS_FILE.write_text("[]")
        with pytest.raises(RuntimeError, match="v2.1"):
            check_legacy_migrations(store)
        assert _flags(store) == ["", ""], "拒绝时不能顺手补标记"

    def test_users_json_with_migration_backup_counts_as_imported(self, store):
        """导入过、之后整库清空（reset_all 连标记一起删）——按全新安装处理。"""
        users_mod.USERS_FILE.write_text("[]")
        backup = users_mod.USERS_FILE.with_name("users.json.migrated.20260516T000000Z.bak")
        backup.write_text("[]")
        check_legacy_migrations(store)
        assert _flags(store) == ["1", "1"]

    def test_users_without_flags_are_refused(self, store):
        """v2.1 之前的库：用户在，回填的凭据可能还没洗。"""
        _seed(store, "u1")
        store.set_meta(USERS_MIGRATION_META_KEY, "1")
        with pytest.raises(RuntimeError):
            check_legacy_migrations(store)
