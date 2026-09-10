"""每个 ListingFilter 字段都要在**每个**构造点被读到——表单三处 + 加载一处。

为什么需要这条通用守卫
----------------------
`app/forms/user_form.py` 里有三处构造 ListingFilter：
`build_user_from_form` 里两处（通知过滤 + 自动预订过滤）、
`build_user_from_form_self` 里一处（用户改自己的）。

漏掉任何一处的表现是**静默丢数据**：用户在手机 API 上设过的条件，从面板保存
一次就被清空——页面不报错，字段也还在模型里，只是没人从表单里读它。

加载侧 `users._lf_from_dict` 是同一个问题的第四处，而且更糟：它对**所有**来源
生效，手机 API 设的值也一样被吞——写进库了，读回来永远是空。

这不是假设。`allowed_sources` 就这么丢过一次（2026-08-04，那处的注释还留着），
`allowed_buildings` 加进来的时候我漏了表单第三处和加载那处。三次都是「加字段的人
只改了他正在看的那个构造点」。

所以这条测试不针对某个字段，而是断言**集合相等**：新增字段却没在三处都解析，
它就会红。
"""
import inspect
import re
from dataclasses import fields

import pytest

from config import ListingFilter
import app.forms.user_form as uf

#: 不由表单直接解析的字段，连同原因。加进来要写清楚为什么。
_NOT_FROM_FORM: dict[str, str] = {
    "allowed_energy": "走 _sanitize_energy()，不是 _lv()——它是单值不是列表",
    "max_rent": "走 _fv()",
    "min_area": "走 _fv()",
    "min_floor": "走 _iv()",
}


def _filter_blocks() -> dict[str, str]:
    """把三处 ListingFilter(...) 的源码切出来，形如 {标签: 源码}。"""
    src = inspect.getsource(uf)
    blocks: dict[str, str] = {}
    for i, m in enumerate(re.finditer(r"ListingFilter\(", src)):
        start = m.end()
        depth, j = 1, start
        while j < len(src) and depth:
            if src[j] == "(":
                depth += 1
            elif src[j] == ")":
                depth -= 1
            j += 1
        blocks[f"构造点{i + 1}"] = src[start:j]
    return blocks


def test_there_are_exactly_three_construction_sites():
    """数量本身要钉住：多出来一处而没人回来看这条测试，覆盖就又漏了。"""
    blocks = _filter_blocks()
    assert len(blocks) == 3, (
        f"ListingFilter 的构造点变成了 {len(blocks)} 处。新增的那处也要解析全部字段，"
        f"确认之后把这个数字改掉。")


@pytest.mark.parametrize("label", sorted(_filter_blocks()))
def test_every_list_field_is_parsed_at_every_site(label):
    block = _filter_blocks()[label]
    expected = {f.name for f in fields(ListingFilter)
                if f.name not in _NOT_FROM_FORM}
    missing = sorted(n for n in expected if f"{n}=" not in block)
    assert not missing, (
        f"{label} 没有解析：{missing}——用户在别处设过的这些条件，"
        f"从这个表单保存一次就会被清空（静默丢数据）")


def test_the_exemption_list_only_names_real_fields():
    """豁免清单不能引用已经改名/删掉的字段，否则它会悄悄豁免掉一个真字段。"""
    names = {f.name for f in fields(ListingFilter)}
    stale = sorted(set(_NOT_FROM_FORM) - names)
    assert not stale, f"豁免清单里的字段已不存在：{stale}"


def test_the_load_path_reads_every_field():
    """`users._lf_from_dict` 漏字段 = 写得进库、读不回来。

    比表单漏更糟：表单只影响从面板保存的人，这里影响**所有**来源，
    手机 API 设的值一样被吞。
    """
    import users

    src = inspect.getsource(users._lf_from_dict)
    missing = sorted(f.name for f in fields(ListingFilter)
                     if f"{f.name}=" not in src)
    assert not missing, (
        f"_lf_from_dict 没有读：{missing}——这些字段写得进库，但每次加载都变回默认值")


def test_a_filter_survives_a_round_trip_through_the_dict():
    """端到端反证：光看源码不够，真存真取一次。"""
    import users
    from dataclasses import asdict

    f = ListingFilter(allowed_buildings=["Teteringsedijk 120"],
                      allowed_sources=["plaza"], max_rent=900.0)
    assert users._lf_from_dict(asdict(f)) == f
