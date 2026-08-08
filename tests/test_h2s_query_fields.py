"""
H2S GraphQL 查询里的字段必须真的被用到。

响应体是按天计的代理流量：每轮两个城市、一天 543 轮。往 ``items`` 里多加
一个字段，成本是 543 × 条数 × 字段大小，每天都要付。

2026-08-07 实测这笔账：

    完整查询   2,096 B/条      92 MB/天
    裁剪后       583 B/条      26 MB/天

差额 70% 是 ``media_gallery``——平均 10.8 张图的 URL，取回来直接丢掉，
listings 表连图片列都没有。同时躺着的还有 ``city``（城市名是入参）和
``minimum_stay``。三个字段谁都没读过，白付了不知道多久。

守卫做的事：把查询 ``items`` 块里的顶层字段，和 ``_to_listing`` 里出现过的
字符串字面量比对。加了字段却不读，这里就失败。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "scrapers" / "holland2stay.py"


def _items_fields() -> set[str]:
    """抽出 _GQL_QUERY 中 ``items { ... }`` 的顶层字段名。

    嵌套块（``price_range { ... }``）只记块名本身，不下钻——子字段是 H2S
    schema 要求的结构，不由 _to_listing 逐个点名。
    """
    src = _SRC.read_text(encoding="utf-8")
    start = src.index("items {") + len("items {")
    depth, i = 1, start
    while depth:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    body = src[start:i - 1]

    fields: set[str] = set()
    depth = 0
    for line in body.splitlines():
        tok = line.strip()
        if not tok:
            continue
        if depth == 0:
            m = re.match(r"^([a-z_][a-z0-9_]*)", tok)
            if m:
                fields.add(m.group(1))
        depth += tok.count("{") - tok.count("}")
    return fields


def _to_listing_strings() -> set[str]:
    """``_to_listing`` 函数体内出现的所有字符串字面量。"""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_to_listing"
    )
    return {
        n.value for n in ast.walk(fn)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }


class TestQueryFieldsAreUsed:
    def test_every_queried_field_is_read(self):
        unused = sorted(_items_fields() - _to_listing_strings())
        assert not unused, (
            "这些字段请求了但 _to_listing 从不读，每轮都在白付流量: "
            f"{unused}。确认要加就先在 _to_listing 里用上它。"
        )

    def test_dropped_fields_stay_dropped(self):
        """2026-08-07 裁掉的三个字段，别又被加回来。"""
        fields = _items_fields()
        for f in ("media_gallery", "city", "minimum_stay"):
            assert f not in fields, (
                f"{f} 已于 2026-08-07 因无人读取被裁掉（media_gallery 一项占"
                "响应体 70%）。真要加回来，先让 _to_listing 用上它。"
            )

    def test_parser_actually_finds_fields(self):
        """守卫别退化成空集合——空集合与任何东西求差都是空。"""
        fields = _items_fields()
        assert len(fields) >= 15, f"字段解析失效，只抽到 {fields}"
        assert "sku" in fields and "price_range" in fields

    def test_required_fields_present(self):
        """反向：_to_listing 依赖的字段不能被误删。"""
        fields = _items_fields()
        for f in ("sku", "url_key", "available_to_book", "basic_rent",
                  "available_startdate", "price_range"):
            assert f in fields, f"_to_listing 依赖 {f}，查询里却没有"
