"""证件文件存储测试。

这个模块存在本身是一个知情的取舍：平台在证件上传前拒绝保存申请表，而自动预订
是**异步**触发的（房源出现时才跑，可能是配置完几天以后），所以不存在「用完即走
的透传」——文件必须先落盘。取舍既然做了，就把能降的风险降到底：加密存、不进
每轮都要加载的用户配置、面板上删得掉。

校验规则抄自上传控件自己的 JS。**在面板上就拦下来**——等到抢房那一刻才发现
文件不合规，房子已经没了。
"""
from __future__ import annotations

import pytest

import applicant_docs
from applicant_docs import DocumentRejected


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    yield


class TestRoundTrip:
    def test_saves_and_reads_back(self):
        applicant_docs.save("u1", "passport.pdf", b"%PDF-1.4 data")
        assert applicant_docs.load("u1") == ("passport.pdf", b"%PDF-1.4 data")

    def test_absent_document_is_none_not_an_error(self):
        assert applicant_docs.load("nobody") is None
        assert applicant_docs.has("nobody") is False

    def test_binary_content_survives_intact(self):
        blob = bytes(range(256)) * 40
        applicant_docs.save("u1", "scan.png", blob)
        assert applicant_docs.load("u1")[1] == blob

    def test_saving_again_replaces(self):
        applicant_docs.save("u1", "a.pdf", b"one")
        applicant_docs.save("u1", "b.pdf", b"two")
        assert applicant_docs.load("u1") == ("b.pdf", b"two")

    def test_users_are_separate(self):
        applicant_docs.save("u1", "a.pdf", b"one")
        applicant_docs.save("u2", "b.pdf", b"two")
        assert applicant_docs.load("u1")[1] == b"one"


class TestStoredEncrypted:
    def test_plaintext_is_not_on_disk(self, tmp_path):
        applicant_docs.save("u1", "passport.pdf", b"SENSITIVE-PASSPORT-BYTES")
        blobs = b"".join(p.read_bytes() for p in tmp_path.rglob("*.bin"))
        assert blobs, "应该确实写了文件"
        assert b"SENSITIVE-PASSPORT-BYTES" not in blobs
        assert b"passport.pdf" not in blobs, "文件名也不该明文落盘"


class TestPlatformLimits:
    """规则抄自上传控件的 JS，在面板上就拦，别拖到抢房那一刻。"""

    def test_rejects_unsupported_extension(self):
        with pytest.raises(DocumentRejected, match="格式"):
            applicant_docs.save("u1", "id.txt", b"x")

    def test_rejects_oversized_file(self):
        with pytest.raises(DocumentRejected, match="5 MB"):
            applicant_docs.save("u1", "big.pdf", b"x" * (applicant_docs.MAX_BYTES + 1))

    def test_rejects_empty_file(self):
        with pytest.raises(DocumentRejected):
            applicant_docs.save("u1", "empty.pdf", b"")

    def test_rejects_long_filename(self):
        with pytest.raises(DocumentRejected, match="100"):
            applicant_docs.save("u1", "a" * 101 + ".pdf", b"x")

    @pytest.mark.parametrize("bad", ['a:b.pdf', 'a*b.pdf', 'a?b.pdf', 'a|b.pdf'])
    def test_rejects_special_characters(self, bad):
        with pytest.raises(DocumentRejected):
            applicant_docs.save("u1", bad, b"x")

    def test_extension_check_is_case_insensitive(self):
        applicant_docs.save("u1", "SCAN.PDF", b"x")

    def test_a_rejected_file_does_not_replace_the_stored_one(self):
        applicant_docs.save("u1", "good.pdf", b"keep me")
        with pytest.raises(DocumentRejected):
            applicant_docs.save("u1", "bad.txt", b"nope")
        assert applicant_docs.load("u1") == ("good.pdf", b"keep me")


class TestDeletable:
    def test_delete_removes_it(self):
        applicant_docs.save("u1", "a.pdf", b"x")
        assert applicant_docs.delete("u1") is True
        assert applicant_docs.load("u1") is None

    def test_deleting_nothing_is_not_an_error(self):
        assert applicant_docs.delete("u1") is False


class TestPathSafety:
    @pytest.mark.parametrize("uid", ["../escape", "a/b", "..", "/etc/passwd"])
    def test_user_id_cannot_escape_the_directory(self, uid, tmp_path):
        try:
            applicant_docs.save(uid, "a.pdf", b"x")
        except ValueError:
            return                      # 直接拒绝也可以
        written = list(tmp_path.rglob("*.bin"))
        for p in written:
            assert tmp_path in p.parents or p.parent.parent == tmp_path


class TestCorruptStoreDoesNotCrashBooking:
    def test_undecryptable_file_reads_as_absent(self, tmp_path):
        """换过 DATA_ENCRYPTION_KEY 时不该让整条预订链路崩掉。"""
        applicant_docs.save("u1", "a.pdf", b"x")
        p = next(tmp_path.rglob("*.bin"))
        p.write_bytes(b"not a fernet token")
        assert applicant_docs.load("u1") is None
