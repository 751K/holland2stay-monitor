"""上传前的本地体检——「先删后传」唯一的安全阀。

这个工具的替换顺序改过三次，值得把账记清楚：

1. **先删后传**。2026-09-04 第一次实跑就把 en-US 下用户手动传的八张删光，而
   紧接着的 PUT 400 失败（给预签名 URL 发了 Authorization 头），集合清空，
   那八张再也找不回来。
2. **先传后删**。修好了上面那条，但引进了新的边界：App Store Connect 每组
   上限 10 张，``旧 + 新`` 一超就永远传不完。6 张旧 iPad 图 + 7 张新的 = 13，
   传到第 4 张拿到 ``Too many screenshots``。这个错误只有对着一个已经有 6 张
   的集合跑才会出现——空集合上怎么试都是绿的。
3. **边传边删**。两条边界都守住了，代价是十来行状态机。
4. **回到先删后传**（现在），这是一个有前提的选择：这批图由 Xcode Cloud 生成、
   本地有副本，删错了重跑一次就回来。第 1 条的伤害不在「空了」，在于那八张
   是人手做的、没有副本。

所以现在的安全阀不是「集合永不为空」，而是**删之前先把这批文件检查一遍**：
0 字节、不是 PNG、超过 10 张，这些都在删任何东西之前就停下。

⚠️ 哪天这个工具要去传一批不可复现的图，这个取舍就不成立了——那时该回到
第 3 条，而不是把这些注释删掉。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _mod():
    path = ROOT / "tools" / "asc" / "asc_screenshots.py"
    spec = importlib.util.spec_from_file_location("asc_screenshots", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def asc():
    return _mod()


def _png(dirpath: Path, name: str, size: int = 64) -> Path:
    p = dirpath / name
    p.write_bytes(_PNG_MAGIC + b"\x00" * size)
    return p


def test_a_healthy_batch_passes(asc, tmp_path):
    paths = [_png(tmp_path, f"{i:02d}.png") for i in range(7)]
    asc.preflight(paths)          # 不抛就是通过


def test_empty_batch_is_refused(asc):
    with pytest.raises(asc.PreflightError):
        asc.preflight([])


def test_more_than_the_cap_is_refused_before_anything_is_deleted(asc, tmp_path):
    """11 张在删光旧图之后也传不完，所以现在就停。

    这正是第 2 版撞上的那堵墙，只是那时是在删/传到一半才撞上。
    """
    paths = [_png(tmp_path, f"{i:02d}.png") for i in range(11)]
    with pytest.raises(asc.PreflightError) as e:
        asc.preflight(paths)
    assert "10" in str(e.value)


def test_exactly_the_cap_is_allowed(asc, tmp_path):
    asc.preflight([_png(tmp_path, f"{i:02d}.png") for i in range(10)])


def test_zero_byte_file_is_caught(asc, tmp_path):
    good = _png(tmp_path, "00.png")
    bad = tmp_path / "01.png"
    bad.write_bytes(b"")
    with pytest.raises(asc.PreflightError) as e:
        asc.preflight([good, bad])
    assert "01.png" in str(e.value) and "0 字节" in str(e.value)


def test_a_file_that_is_not_a_png_is_caught(asc, tmp_path):
    """扩展名对不代表内容对。提取脚本换过一次实现，产出格式不该没人查。"""
    good = _png(tmp_path, "00.png")
    bad = tmp_path / "01.png"
    bad.write_bytes(b"<html>nope</html>")
    with pytest.raises(asc.PreflightError) as e:
        asc.preflight([good, bad])
    assert "不是 PNG" in str(e.value)


def test_a_missing_file_is_caught(asc, tmp_path):
    good = _png(tmp_path, "00.png")
    with pytest.raises(asc.PreflightError):
        asc.preflight([good, tmp_path / "does-not-exist.png"])


def test_the_error_names_every_bad_file_not_just_the_first(asc, tmp_path):
    """一次说清，别让人删一次跑一次。"""
    good = _png(tmp_path, "00.png")
    (tmp_path / "01.png").write_bytes(b"")
    (tmp_path / "02.png").write_bytes(b"not a png")
    with pytest.raises(asc.PreflightError) as e:
        asc.preflight([good, tmp_path / "01.png", tmp_path / "02.png"])
    msg = str(e.value)
    assert "01.png" in msg and "02.png" in msg


def test_preflight_says_nothing_was_deleted(asc, tmp_path):
    """错误信息要让人放心：这一步失败时旧图还在。"""
    bad = tmp_path / "00.png"
    bad.write_bytes(b"")
    with pytest.raises(asc.PreflightError) as e:
        asc.preflight([bad])
    assert "未删除" in str(e.value)
