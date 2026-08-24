"""申请人档案的字段加密。

这张档案是给 RENTCafe 租房申请表填的，服务端存着：

    id_number       身份证 / 护照号
    date_of_birth   出生日期
    nationality / place_of_birth / gender / phone / student_number
    ever_evicted / ever_convicted / criminal_charges   ← GDPR 第 10 条数据

2026-08-24 之前只有 date_of_birth / address / id_number 三项加密，**犯罪记录和
国籍是明文**。改成「默认加密 + 例外清单」之后，忘记维护清单的后果从「明文存了
一项敏感数据」变成「多加密了一个不敏感字段」——两种疏忽的代价不对称。
"""
from __future__ import annotations

import dataclasses
import json

import pytest

from config import (
    ApplicantProfile,
    _PLAINTEXT_PROFILE_FIELDS,
    profile_field_is_encrypted,
)
from crypto import decrypt, encrypt

_ALL = [f.name for f in dataclasses.fields(ApplicantProfile)]


class TestWhichFieldsAreEncrypted:
    def test_default_is_encrypted(self):
        """新加字段默认加密——这是这次改动的全部要点。"""
        assert profile_field_is_encrypted("some_field_added_next_year")

    @pytest.mark.parametrize("field", [
        "id_number", "date_of_birth", "address", "address_line2",
        "nationality", "place_of_birth", "gender", "phone",
        "student_number", "postcode", "postcode_city", "city",
        "first_name", "middle_name", "last_name", "university",
    ])
    def test_personal_data_is_encrypted(self, field):
        assert field in _ALL, f"{field} 不再是档案字段了，用例要跟着改"
        assert profile_field_is_encrypted(field)

    @pytest.mark.parametrize("field", ["ever_evicted", "ever_convicted", "criminal_charges"])
    def test_criminal_history_is_encrypted(self, field):
        """GDPR 第 10 条数据。改动前这三项是明文，比身份证号还敏感。"""
        assert profile_field_is_encrypted(field)

    def test_plaintext_list_is_small_and_justified(self):
        """例外清单每多一项就多一分风险，别让它悄悄变长。"""
        assert _PLAINTEXT_PROFILE_FIELDS == {
            "no_middle_name", "min_lease_term", "housing_type",
        }

    def test_every_plaintext_field_actually_exists(self):
        for f in _PLAINTEXT_PROFILE_FIELDS:
            assert f in _ALL, f"例外清单里的 {f} 已经不是档案字段，属于陈旧配置"


class TestRoundTrip:
    def test_saved_profile_is_encrypted_at_rest(self):
        from users import UserConfig, _user_to_row
        # 明文取足够长且可区分的值：短字符串（"No"）会**碰巧出现在别的字段的
        # base64 密文里**，让"明文没泄漏"这条断言假失败。第一版就踩了这个。
        prof = {"id_number": "IDNUM-QQQQ-7788", "criminal_charges": "CRIMREC-ZZZZ",
                "nationality": "NATION-WWWW", "no_middle_name": True}
        u = UserConfig(id="u1", name="t")
        u.auto_book.applicant_profile = ApplicantProfile(**{
            k: v for k, v in prof.items()
        })
        row = _user_to_row(u)
        stored = json.loads(row["auto_book_json"])["applicant_profile"]
        for k in ("id_number", "criminal_charges", "nationality"):
            assert stored[k].startswith("$F$"), f"{k} 落库时没加密"
            assert prof[k] not in row["auto_book_json"], f"{k} 的明文出现在了库里"
        assert stored["no_middle_name"] is True, "布尔的例外字段不该被加密"

    def test_round_trip_returns_the_original(self):
        from users import _profile_from_dict
        raw = {"id_number": encrypt("AB1234567"),
               "criminal_charges": encrypt("No"),
               "no_middle_name": True}
        got = _profile_from_dict(raw)
        assert got.id_number == "AB1234567"
        assert got.criminal_charges == "No"
        assert got.no_middle_name is True

    def test_legacy_plaintext_still_reads(self):
        """decrypt 对无前缀的值向后兼容，老数据不能因为改了清单就读不出来。"""
        from users import _profile_from_dict
        got = _profile_from_dict({"nationality": "Chinese"})
        assert got.nationality == "Chinese"


class TestEncryptIsIdempotent:
    """迁移和保存路径都可能对同一个值调两次。"""

    def test_double_encrypt_is_a_noop(self):
        once = encrypt("Nederlandse")
        assert encrypt(once) == once

    def test_still_decrypts_after_double_call(self):
        assert decrypt(encrypt(encrypt("AB1234567"))) == "AB1234567"

    def test_empty_stays_empty(self):
        assert encrypt("") == ""


class TestMigration:
    @pytest.fixture(autouse=True)
    def _fresh(self):
        from mstorage._base import StorageBase
        StorageBase._migrated_paths.clear()
        yield
        StorageBase._migrated_paths.clear()

    def _seed(self, path, profile):
        from mstorage import Storage
        st = Storage(path)
        st.conn.execute(
            "INSERT INTO user_configs (id, name, enabled, auto_book_json,"
            " created_at, updated_at) VALUES (?,?,1,?,?,?)",
            ("u1", "t", json.dumps({"applicant_profile": profile}, ensure_ascii=False),
             "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        )
        st.conn.commit(); st.close()

    def _reopen_profile(self, path):
        from mstorage import Storage
        from mstorage._base import StorageBase
        StorageBase._migrated_paths.clear()
        st = Storage(path)
        row = st.conn.execute(
            "SELECT auto_book_json FROM user_configs WHERE id='u1'").fetchone()
        st.close()
        return json.loads(row["auto_book_json"])["applicant_profile"]

    def test_plaintext_at_rest_gets_encrypted(self, tmp_path):
        p = tmp_path / "listings.db"
        self._seed(p, {"criminal_charges": "No", "nationality": "Chinese",
                       "no_middle_name": True})
        got = self._reopen_profile(p)
        assert got["criminal_charges"].startswith("$F$")
        assert got["nationality"].startswith("$F$")
        assert got["no_middle_name"] is True

    def test_values_survive_the_migration(self, tmp_path):
        p = tmp_path / "listings.db"
        self._seed(p, {"id_number": "AB1234567"})
        assert decrypt(self._reopen_profile(p)["id_number"]) == "AB1234567"

    def test_already_encrypted_is_left_alone(self, tmp_path):
        """幂等：跑第二遍不能套第二层。"""
        p = tmp_path / "listings.db"
        self._seed(p, {"id_number": encrypt("AB1234567")})
        first = self._reopen_profile(p)["id_number"]
        assert self._reopen_profile(p)["id_number"] == first
        assert decrypt(first) == "AB1234567"

    def test_second_run_writes_nothing(self, tmp_path):
        """已经加密过的行不该被反复重写。

        encrypt() 幂等之后，少了「跳过已加密」那道判断结果也一样对，但每次启动
        都会对每一行发一次无谓的 UPDATE。这条用例守的是**没有多余写入**，
        不是结果正确——后者已经被上一条守着了。

        用 SQLite 自带的 ``total_changes``（本连接迄今改过多少行）来数：
        caplog 依赖日志器配置，整套跑时别的用例改过 propagate 就会静默失效
        （第一版这么挂过）；而 sqlite3.Connection 是 C 类型，方法没法 monkeypatch
        （第二版这么挂过）。
        """
        from mstorage import Storage
        from mstorage._base import StorageBase

        p = tmp_path / "listings.db"
        self._seed(p, {"id_number": "AB1234567"})

        StorageBase._migrated_paths.clear()
        st1 = Storage(p)
        first = st1.conn.total_changes
        st1.close()
        assert first > 0, "第一次开库应当真的迁移了"

        StorageBase._migrated_paths.clear()
        st2 = Storage(p)
        second = st2.conn.total_changes
        st2.close()
        assert second == 0, f"第二次开库仍然写了 {second} 行"

    def test_broken_json_does_not_block_startup(self, tmp_path):
        from mstorage import Storage
        from mstorage._base import StorageBase
        p = tmp_path / "listings.db"
        st = Storage(p)
        st.conn.execute(
            "INSERT INTO user_configs (id, name, enabled, auto_book_json,"
            " created_at, updated_at) VALUES (?,?,1,?,?,?)",
            ("u1", "t", "{not json", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
        st.conn.commit(); st.close()
        StorageBase._migrated_paths.clear()
        Storage(p).close()          # 不许抛
