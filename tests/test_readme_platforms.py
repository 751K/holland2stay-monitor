"""README 里的平台清单要跟得上 ``KNOWN_SOURCES``。

2026-09-02 接入 Student Experience 和 Plaza，两份 README 一直写着「五个平台」，
到 2026-09-10 才发现——**整整八天**。而这期间平台表、开头那段介绍、特性表三处都
在说同一个过时的数字。

漏得掉是因为 README 和代码之间没有任何联系。测试全绿、部署照常、面板上七个平台
的卡片都在，只有对外的第一份文档在说五个。对一个自部署项目来说，README 就是别人
判断「它抓不抓我要的那家」的唯一依据。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from config import KNOWN_SOURCES, SOURCE_DISPLAY_NAMES

ROOT = Path(__file__).resolve().parent.parent
READMES = ("README.md", "README_cn.md")

#: 中文数字，README 用它写平台总数。
_CN_NUM = {5: "五", 6: "六", 7: "七", 8: "八", 9: "九", 10: "十"}
_EN_NUM = {5: "Five", 6: "Six", 7: "Seven", 8: "Eight", 9: "Nine", 10: "Ten"}


@pytest.fixture(scope="module", params=READMES)
def readme(request) -> tuple[str, str]:
    name = request.param
    return name, (ROOT / "docs" / name).read_text(encoding="utf-8")


def test_every_source_is_named(readme):
    """每个接入的平台都要在 README 里出现。"""
    name, text = readme
    missing = [SOURCE_DISPLAY_NAMES.get(s, s) for s in KNOWN_SOURCES
               if SOURCE_DISPLAY_NAMES.get(s, s) not in text]
    assert not missing, (
        f"{name} 没提到这些已接入的平台：{missing}——"
        "对自部署的人来说，README 是判断「抓不抓我要的那家」的唯一依据")


def test_the_platform_table_has_a_row_for_each(readme):
    """平台覆盖表要一家一行。

    只在介绍里列举名字不够——那张表才是讲覆盖范围和预订能力的地方。
    """
    name, text = readme
    marker = "### 平台覆盖" if name.endswith("_cn.md") else "| Platform | Coverage"
    assert marker in text, f"{name} 里找不到平台覆盖表"
    table = text[text.index(marker):]
    table = table[:table.index("\n\n", table.index("|---"))]
    missing = [d for s in KNOWN_SOURCES
               if (d := SOURCE_DISPLAY_NAMES.get(s, s)) not in table]
    assert not missing, f"{name} 的平台覆盖表少了：{missing}"


def test_the_stated_count_matches(readme):
    """写死的平台总数要对。

    「五个平台」这种话最容易过时——加一家平台时没人会想到回来改一个数字。
    """
    name, text = readme
    n = len(KNOWN_SOURCES)
    want = f"{_CN_NUM[n]}个平台" if name.endswith("_cn.md") else f"{_EN_NUM[n]} platforms"
    stale = [w for k, w in (
        (_CN_NUM if name.endswith("_cn.md") else _EN_NUM).items())
        if k != n and (f"{w}个平台" if name.endswith("_cn.md") else f"{w} platforms") in text]
    assert not stale, (
        f"{name} 写着「{stale[0]}个平台」，实际是 {n} 个")
    assert want in text, f"{name} 没写出平台总数「{want}」"


def test_docs_index_lists_every_platform_recon_file(readme):
    """文档索引里不能漏掉实际存在的侦察文档。"""
    name, text = readme
    have = {p.name for p in (ROOT / "docs").glob("*.md")}
    recon = {n for n in have
             if n.startswith(("H2S", "XIOR", "OURDOMAIN", "SCRAPING_RECON"))}
    missing = sorted(n for n in recon if f"({n})" not in text)
    assert not missing, f"{name} 的文档索引漏了：{missing}"
